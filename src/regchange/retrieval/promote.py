"""위임 관계를 타고 상위 정책 조항을 후보로 올린다 — R-22 의 대응 경로.

목적:
    1차 검색(top-k)에 잡힌 하위 문서(지침·절차서)를 근거로, 그 문서가 위임받은 **상위
    문서의 조항**을 후보 집합에 추가한다. 추가된 문단은 `DELEGATION_PROMOTED` 로 표시되고
    무엇 때문에 올라왔는지(`PromotionBasis`)를 함께 들고 다닌다.

구현 이유:
    **검색 파라미터가 아니라 문서 관계로 푼다.** R-22 는 정책 최상위 문서의 조가 평균
    95자짜리 추상 선언이라 수백 자 개정문과 어휘·의미 밀도가 구조적으로 맞지 않는
    문제다(`docs/10-retrieval-evaluation-results.md` §5.3). 짧은 문단 가산점이나 임계값
    조정으로 풀면 **한 실패를 고치려고 지표 전체를 움직이는 상수**가 들어간다. 문서 관계는
    유사도 계산에 손대지 않으므로 기존 측정을 흔들지 않는다.

    **조가 지목된 위임은 재검색하지 않는다.** `ISP-PROC-002` 제1조는 `「정보보호 관리지침」
    (ISP-GUIDE-002) 제35조에서 위임된 사항` 이라고 조까지 적었다. 문서가 스스로 말한 것이
    우리 검색보다 정확하다 — ADR-003 이 조번호 이동에서 **명시 표기를 1순위 근거**로 둔
    것과 같은 판단이다. 다만 지목된 조도 **후보이지 확정이 아니다.** 그 조가 이번 개정과
    무관할 수 있으며, 판정은 gate 와 사람이 한다.

    **문서 단위 위임은 상위 문서로 범위를 좁혀 같은 질의를 다시 던진다.** 상위 문서의 조
    전체를 올리면(`ISP-POL-001` 18조) 재현율은 올라가지만 정밀도가 무너지고 검토자가 볼
    것이 늘어난다. R-22 는 재현율 문제인데 정밀도를 희생해 푸는 것은 교환이지 해결이
    아니다. 장(章) 주제 대응표를 만드는 방법도 있으나, 그 표는 근거 없는 규약을 하나 더
    만드는 것이고 문서가 늘면 표도 늘어난다. 좁혀 재검색하는 것은 **새 규약을 만들지
    않는다** — 같은 질의를 좁은 후보군에 다시 던질 뿐이다.

    **1단계만 올라간다.** `ISP-PROC-002 → ISP-GUIDE-002 → ISP-POL-001` 로 이어지는 다단
    승격은 하지 않는다. R-22 로 관측된 3항목(`ISP-POL-001` 제5·8·15조)은 전부 1단이면
    닿는다 — 잡힌 문서가 `ISP-GUIDE-002`·`ISP-GUIDE-003` 이고 그 상위가 곧 `ISP-POL-001`
    이다. 관측되지 않은 필요를 위해 후보를 늘리지 않는다.

트레이드오프:
    - **점수가 두 검색 사이에서 비교 가능하지 않다.** 좁힌 재검색은 후보 집합이 달라
      RRF 순위도 BM25 의 IDF 도 달라진다. 승격 문단의 `score` 를 1차 결과의 `score` 와
      같은 척도로 읽으면 안 되며, 그래서 `RetrievalSource` 로 경로를 표시한다.
      표시 없이 섞었다면 이 비교 불가능성이 어디에도 드러나지 않았을 것이다.
    - `top_n` 이 상수다. **측정 없이 정하지 않는다** — 값은 골든셋 스윕
      (`evals/runners/delegation_sweep.py`)이 재현율과 정밀도를 함께 낸 뒤 정한다.
    - 승격은 후보를 늘리므로 프롬프트 입력 토큰이 는다. 늘어난 만큼 F-6(그럴듯하게 틀림)의
      노출면도 넓어진다 — 승격 문단이 함정이 되는 경우가 그것이다. 그래서 승격분을
      **따로 세어** 정밀도 하락을 측정한다.

엣지 케이스:
    - **1차 결과가 0건**: 승격도 0건이다. 위임은 잡힌 문서에서 출발하므로 출발점이 없다.
      `promoted=0` 인 리포트를 남긴다 — "시도했으나 올릴 것이 없었다"와 "시도하지 않았다"는
      다른 사실이다.
    - **이미 1차에 있는 상위 문단**: 승격하지 않는다. 같은 문단이 두 번 들어가면 검증
      정답 집합에 중복이 생기고, 중복은 인용 검증을 통과시키는 방향으로 작용한다.
    - **여러 하위 문서가 같은 상위 문서를 가리킴**: 상위 문서당 한 번만 재검색한다.
      근거(`PromotionBasis`)에는 **1차 순위가 가장 높은** 하위 문단을 담는다.
    - **지목된 조가 그 시점에 존재하지 않음**: 승격하지 않고 경고를 남긴다. 조용히 넘기면
      "위임은 있는데 대상이 없다"는 사실이 사라진다.
    - **상위 문서를 적재하지 않음**(dangling): 건너뛰고 리포트에 남긴다.
    - **위임 그래프에 순환**: `DelegationError` 가 전파된다. 승격을 부분적으로 수행하지
      않는다 (`delegation.py` 참조).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from regchange.adapters.embedding import EmbeddingClient
from regchange.guards.killswitch import SwitchGate
from regchange.retrieval.delegation import (
    DELEGATION_ARTICLE_NO,
    DelegationEdge,
    DelegationGraph,
    build_delegation_graph,
)
from regchange.retrieval.models import (
    DelegationReport,
    PromotionBasis,
    PromotionMechanism,
    RetrievalResult,
    RetrievalSource,
    RetrievedChunk,
    SearchMode,
)
from regchange.retrieval.search import search

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 2
"""문서 단위 위임에서 상위 문서 재검색으로 올리는 조의 수.

**골든셋 스윕으로 정했다** — `evals/runners/delegation_sweep.py`, 결과는
`docs/12-delegation-promotion-results.md` §2. N 을 1·2·3 으로 돌려 R-22 3항목의 회수 수와
승격분이 만든 오탐(정답이 아닌 승격 문단) 수를 **함께** 보고 고른 값이다. 이 상수를
바꾸려는 사람은 그 표를 먼저 읽어야 한다 — 재현율만 보고 키우면 검토 큐가 소음으로 차고,
ADR-003 이 「틀렸음을 알게 되는 신호 1번」으로 적은 상태(후보 확정률이 낮고 대부분 반려)가
발동한다.

| N | 재현율@k | R-22 회수 | 승격 계 | 승격 정답 | 승격 오탐 |
|---|---|---|---|---|---|
| 0 | 0.7667 | 0/3 | 0 | 0 | 0 |
| 1 | 0.7667 | 0/3 | 18 | 0 | 18 |
| **2** | **0.8167** | **1/3** | 33 | 1 | 32 |
| 3 | 0.8167 | 1/3 | 49 | 1 | 48 |

N=1 은 **회수가 0인데 후보만 18개 늘린다** — 순수 손해다. N=3 은 N=2 와 재현율이 같고
무관 후보만 16개 더 붙는다. 2 만이 회수를 만들며, 그 회수는 **1항목뿐이다.**"""

_FIRST_ARTICLE_SQL = """
SELECT d.doc_id,
       (SELECT p.text_raw
          FROM policy_paragraph p
         WHERE p.document_id = d.id
           AND p.article_no = %(article_no)s
           AND p.known_until = 'infinity') AS first_article
  FROM policy_document d
 WHERE d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s::date
"""
"""문서별 제1조 본문. 없으면 NULL 이며 `missing_article` 로 세어진다."""

_ARTICLE_SQL = """
SELECT p.id,
       d.doc_id,
       d.version,
       p.article_no,
       p.article_title,
       p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity'
   AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s::date
   AND d.doc_id = %(doc_id)s
   AND p.article_no = %(article_no)s
"""
"""지목된 조 하나. 재검색 없이 직접 승격할 때만 쓴다."""


async def load_delegation_graph(
    conn: psycopg.AsyncConnection[Any], *, as_of: dt.date
) -> DelegationGraph:
    """그 시점의 문서들에서 위임 그래프를 읽어 만든다.

    목적:
        DB 에 적재된 제1조 본문을 파서(`delegation.py`)에 넘겨 관계 원장을 만든다.

    구현 이유:
        시점(`as_of`)을 받는다. 위임 관계는 문서 본문에서 파생되므로 **문서가 개정되면
        관계도 바뀐다.** 시점 없이 읽으면 과거 재현이 현재 관계로 이루어지고, 그 오류는
        아무 예외도 내지 않는다 (원칙 6).

    트레이드오프:
        문서 수만큼 서브쿼리가 돈다. 5종 규모에서는 무의미한 비용이고, 매 검색마다
        다시 읽는 대신 캐시를 두면 문서를 개정했을 때 옛 관계가 남는다.

    엣지 케이스:
        - 그 시점에 문서가 하나도 없음: 빈 그래프. 승격은 0건이 된다.
        - 제1조가 없는 문서: `missing_article` 로 센다.
    """
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            _FIRST_ARTICLE_SQL, {"as_of": as_of, "article_no": DELEGATION_ARTICLE_NO}
        )
        rows = await cursor.fetchall()

    first_articles: dict[str, str | None] = {
        str(row["doc_id"]): (str(row["first_article"]) if row["first_article"] else None)
        for row in rows
    }
    graph = build_delegation_graph(first_articles)
    if graph.missing_article:
        logger.warning(
            "제%d조가 없는 문서 %d건 — 적재나 파서를 의심한다: %s",
            DELEGATION_ARTICLE_NO,
            len(graph.missing_article),
            ", ".join(graph.missing_article),
        )
    return graph


async def promote_by_delegation(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    result: RetrievalResult,
    query: str,
    as_of: dt.date,
    top_n: int = DEFAULT_TOP_N,
    client: EmbeddingClient | None = None,
    mode: SearchMode = SearchMode.HYBRID,
    graph: DelegationGraph | None = None,
) -> RetrievalResult:
    """1차 검색 결과에 위임 승격분을 더한 결과를 만든다.

    목적:
        R-22(정책 계층 조항이 문단 유사도로 잡히지 않는다)를 문서 관계로 회수한다.

    구현 이유:
        1차 결과를 **고치지 않고 뒤에 덧붙인다.** 순위를 재계산하면 승격이 1차 결과의
        순서를 바꾸게 되고, 그러면 기존 검색 측정과의 비교가 끊긴다. 승격은 후보를
        **추가**하는 조작이지 순위를 다시 매기는 조작이 아니다.

        `graph` 를 인자로 받을 수 있게 둔다. 골든셋 스윕이 같은 그래프로 N 만 바꿔
        돌려야 하며, 매번 다시 읽으면 측정 사이에 관계가 달라질 여지가 생긴다.

    트레이드오프:
        승격 문단이 1차 문단보다 뒤 순위를 받는다. 실제로는 더 중요한 조항일 수 있지만,
        순위를 섞으면 두 경로의 점수를 비교 가능한 것처럼 다루게 된다. 순서보다
        **경로의 구별**을 지켰다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    if top_n <= 0:
        msg = f"top_n 은 1 이상이어야 한다: {top_n}"
        raise ValueError(msg)

    delegation = graph if graph is not None else await load_delegation_graph(conn, as_of=as_of)

    seen: set[UUID] = {chunk.paragraph_id for chunk in result.chunks}
    best_by_doc: dict[str, RetrievedChunk] = {}
    for chunk in result.chunks:
        current = best_by_doc.get(chunk.doc_id)
        if current is None or chunk.rank < current.rank:
            best_by_doc[chunk.doc_id] = chunk

    promoted: list[RetrievedChunk] = []
    used: list[str] = []
    declared: list[str] = []
    handled_parents: set[str] = set()

    for doc_id in sorted(best_by_doc, key=lambda d: best_by_doc[d].rank):
        via = best_by_doc[doc_id]
        for edge in delegation.parents_of(doc_id):
            label = f"{edge.child_doc_id} → {edge.parent_doc_id}"
            if edge.parent_article_no is not None:
                declared_chunk = await _fetch_declared_article(
                    conn, edge=edge, as_of=as_of, via=via
                )
                if declared_chunk is None or declared_chunk.paragraph_id in seen:
                    continue
                seen.add(declared_chunk.paragraph_id)
                promoted.append(declared_chunk)
                declared.append(f"{label} 제{edge.parent_article_no}조")
                used.append(label)
                continue

            if edge.parent_doc_id in handled_parents:
                continue
            handled_parents.add(edge.parent_doc_id)
            found = await _research_parent(
                conn,
                switches=switches,
                edge=edge,
                via=via,
                query=query,
                as_of=as_of,
                top_n=top_n,
                client=client,
                mode=mode,
                seen=seen,
            )
            if found:
                promoted.extend(found)
                used.append(label)

    merged = tuple(
        chunk.model_copy(update={"rank": index})
        for index, chunk in enumerate([*result.chunks, *promoted], start=1)
    )
    report = DelegationReport(
        top_n=top_n,
        promoted=len(promoted),
        used_edges=tuple(dict.fromkeys(used)),
        declared_article_edges=tuple(dict.fromkeys(declared)),
        skipped_dangling=tuple(
            f"{e.child_doc_id} → {e.parent_doc_id}" for e in delegation.dangling
        ),
        undeclared_docs=delegation.undeclared,
    )
    if delegation.dangling:
        logger.warning(
            "상위 문서를 갖고 있지 않아 건너뛴 위임 %d건: %s",
            len(delegation.dangling),
            ", ".join(report.skipped_dangling),
        )
    logger.info(
        "위임 승격 %d건 (top_n=%d, 간선 %s)", len(promoted), top_n, report.used_edges or "없음"
    )

    return result.model_copy(update={"chunks": merged, "delegation": report})


async def _fetch_declared_article(
    conn: psycopg.AsyncConnection[Any],
    *,
    edge: DelegationEdge,
    as_of: dt.date,
    via: RetrievedChunk,
) -> RetrievedChunk | None:
    """문서가 지목한 상위 조를 그대로 가져온다. 없으면 경고하고 None."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            _ARTICLE_SQL,
            {"as_of": as_of, "doc_id": edge.parent_doc_id, "article_no": edge.parent_article_no},
        )
        row = await cursor.fetchone()

    if row is None:
        logger.warning(
            "위임이 지목한 조가 %s 시점에 없다: %s 제%s조 (근거: %s)",
            as_of,
            edge.parent_doc_id,
            edge.parent_article_no,
            edge.evidence_quote[:60],
        )
        return None

    return RetrievedChunk(
        paragraph_id=UUID(str(row["id"])),
        doc_id=str(row["doc_id"]),
        doc_version=str(row["version"]),
        article_no=int(row["article_no"]),
        article_title=str(row["article_title"]),
        text_raw=str(row["text_raw"]),
        # 재검색을 하지 않았으므로 유사도가 없다. 0.0 은 "점수가 없다"는 표시이며
        # `mechanism=DECLARED_ARTICLE` 이 그 사실을 설명한다. 임의의 점수를 지어내
        # 1차 결과와 섞이게 만들지 않는다.
        score=0.0,
        rank=0,
        source=RetrievalSource.DELEGATION_PROMOTED,
        promotion=PromotionBasis(
            via_doc_id=via.doc_id,
            via_article_no=via.article_no,
            via_rank=via.rank,
            delegation_quote=edge.evidence_quote,
            mechanism=PromotionMechanism.DECLARED_ARTICLE,
        ),
    )


async def _research_parent(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    edge: DelegationEdge,
    via: RetrievedChunk,
    query: str,
    as_of: dt.date,
    top_n: int,
    client: EmbeddingClient | None,
    mode: SearchMode,
    seen: set[UUID],
) -> list[RetrievedChunk]:
    """상위 문서로 범위를 좁혀 같은 질의를 다시 던지고 상위 `top_n` 조를 올린다."""
    scoped = await search(
        conn,
        switches=switches,
        query=query,
        mode=mode,
        limit=top_n,
        as_of=as_of,
        client=client,
        doc_ids=(edge.parent_doc_id,),
    )
    out: list[RetrievedChunk] = []
    for chunk in scoped.chunks:
        if chunk.paragraph_id in seen:
            continue
        seen.add(chunk.paragraph_id)
        out.append(
            chunk.model_copy(
                update={
                    "source": RetrievalSource.DELEGATION_PROMOTED,
                    "promotion": PromotionBasis(
                        via_doc_id=via.doc_id,
                        via_article_no=via.article_no,
                        via_rank=via.rank,
                        delegation_quote=edge.evidence_quote,
                        mechanism=PromotionMechanism.RESEARCHED,
                    ),
                }
            )
        )
    return out
