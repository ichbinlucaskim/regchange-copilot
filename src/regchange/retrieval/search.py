"""사내 규정 문단 검색 — 벡터 · 어휘 · 하이브리드. 모든 검색은 시점을 받는다.

목적:
    개정 조문에서 만든 질의로 사내 규정 문단을 찾아 `RetrievedChunk` 목록을 낸다.
    이 결과의 문단 ID 집합이 이후 인용 검증의 정답 집합이 된다 (원칙 2).

구현 이유:
    **모든 검색 함수가 `as_of` 를 받는다.** 기본값은 "오늘"이지만 재현(replay) 시에는
    과거 시점이 주입된다. 지금은 정책 버전이 하나뿐이라 실질 차이가 없다 — 그럼에도
    지금 넣는 이유는, 나중에 붙이면 호출부를 전부 고쳐야 하고 그 과정에서 시점을
    빠뜨린 경로가 하나 남기 때문이다. 그 하나는 평소에 잘 돌다가 감사 대응에서만
    틀린 답을 낸다 (원칙 6).

    **세 방식을 한 모듈에 둔다.** 같은 후보 집합·같은 시점 필터·같은 중복 검사를
    공유해야 비교가 성립한다. 파일을 나누면 한쪽에만 필터가 추가되는 일이 생기고,
    그 차이는 지표 차이로 나타나 모델 차이로 오독된다.

    **RRF 에 넣는 순위 목록을 자르지 않는다.** 결합 깊이를 상수로 두면 근거 없는
    숫자가 하나 더 생긴다. 후보 집합 전체 순위를 그대로 합치고 마지막에 `limit` 만
    적용한다. 코퍼스가 커져 전체 순위 계산이 부담이 되면 그때 깊이를 도입하고,
    **그 깊이가 재현율에 주는 영향을 함께 측정해서** 기록한다.

트레이드오프:
    - 어휘 검색은 후보 문단 전체를 메모리로 읽어 색인을 만든다(`lexical.py` 참조).
      벡터 검색은 DB 가 정렬한다. 두 축이 서로 다른 곳에서 계산되는 비대칭이 있다.
      한쪽으로 통일하는 대신, 각자 잘하는 곳에 뒀다 — Postgres 에는 한국어 형태소
      분석기가 없고 IDF 도 없으며, 벡터 정렬은 DB 가 훨씬 잘한다.
    - 하이브리드는 두 검색을 모두 돌리므로 지연이 둘의 합이다. 병렬화하지 않았다.
      152 문단에서 전체가 수십 밀리초이고, 병렬화는 측정 대상이 아닌 변수를 하나
      더 넣는다.
    - **`RETRIEVAL_ENABLED` 를 이 함수 진입에서 검사한다 (5단계).** 판정 로직은 `guards`
      한 곳이고 여기서는 부르기만 한다 — 4단계 docstring 이 "여기서 검사하지 않는다"고
      적은 것은 **판정이 두 벌이 되는 것**을 우려한 것이었고, 그것은 일어나지 않았다.
      검사 지점은 오히려 검색 창구인 여기여야 한다. 호출부에 맡기면 새 호출 경로가
      조용히 빠진다 (`switches` 에 기본값을 두지 않는 이유도 같다).

엣지 케이스:
    - 후보 문단이 0건: 빈 결과를 그대로 반환한다. 유사도 임계값을 낮춰 억지로 채우지
      않는다. 빈 결과는 "영향 없음" 판정의 정당한 근거다 (`retrieval/__init__.py`).
      다만 `corpus_size=0` 이면 검색 문제가 아니라 적재 문제이므로 경고를 남긴다.
    - 임베딩이 없는 문단: 벡터 검색의 후보에서 빠진다. 어휘 검색에는 남는다.
      조용히 빠지면 재현율이 모델 탓으로 보이므로, 누락 건수를 경고 로그에 남긴다.
    - 결과에 같은 문단 ID 가 두 번: `SearchError`. 검증 정답 집합의 중복은 인용
      검증을 통과시키는 방향으로 작용하므로 조용히 넘기지 않는다.
    - 빈 질의: `SearchError`. 빈 질의에 대한 top-k 는 "아무거나 k 개"다.
    - `doc_ids=()` (빈 튜플): `SearchError`. "범위를 걸지 않음"(`None`)과 "아무 문서도
      대상이 아님"을 구별한다. 빈 범위를 전체 검색으로 떨어뜨리면 승격이 코퍼스 전체를
      다시 훑으면서 그 사실이 어디에도 드러나지 않는다.
    - 벡터 검색인데 임베딩 클라이언트가 없음: `SearchError`. 조용히 어휘 검색으로
      떨어지지 않는다 — 그러면 측정이 다른 방식을 잰다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from regchange.adapters.embedding import EmbeddingClient
from regchange.guards.killswitch import Switch, SwitchGate
from regchange.retrieval import lexical
from regchange.retrieval.fusion import reciprocal_rank_fusion
from regchange.retrieval.models import RetrievalResult, RetrievedChunk, SearchMode

logger = logging.getLogger(__name__)


class SearchError(RuntimeError):
    """검색을 수행할 수 없거나 결과가 계약을 위반했다."""


_CANDIDATE_SQL = """
SELECT p.id,
       d.doc_id,
       d.version,
       p.article_no,
       p.article_title,
       p.text_raw,
       p.text_norm
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity'
   AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s::date
   AND (%(doc_ids)s::text[] IS NULL OR d.doc_id = ANY(%(doc_ids)s::text[]))
 ORDER BY d.doc_id, p.article_no
"""
"""시점 필터를 건 후보 문단 전체.

`effective_date <= as_of` 만 본다. 정책 문서에는 폐지일 컬럼이 없다 — 폐지는 새
버전으로 표현되며, 여러 버전이 있을 때 어느 것이 그 시점의 현행인지는 버전 선택
문제가 된다. 지금은 문서마다 버전이 하나뿐이라 발생하지 않으며, 두 번째 버전을
넣는 시점에 이 질의와 `policy_document` 의 자연키를 함께 고친다."""

_VECTOR_SQL = """
SELECT p.id,
       1.0 - (e.embedding <=> %(query)s::vector) AS score
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
  JOIN policy_paragraph_embedding e ON e.paragraph_id = p.id
 WHERE p.known_until = 'infinity'
   AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s::date
   AND e.model_id = %(model_id)s
   AND (%(doc_ids)s::text[] IS NULL OR d.doc_id = ANY(%(doc_ids)s::text[]))
 ORDER BY e.embedding <=> %(query)s::vector
"""
"""코사인 거리 오름차순 = 유사도 내림차순. 점수는 `1 - 거리` 로 뒤집어 담는다.

`LIMIT` 을 걸지 않는다. 하이브리드가 전체 순위를 필요로 하고, 152행에서 전수 정렬은
근사 인덱스 없이도 밀리초다. 근사 인덱스를 만들지 않는 이유는 마이그레이션 009 참조 —
지금 재려는 것이 재현율인데 근사 탐색이 그것을 깎는다."""


def _vector_literal(vector: tuple[float, ...]) -> str:
    """질의 벡터를 pgvector 리터럴로 만든다. `index.py` 와 같은 규칙이다."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


async def _candidates(
    conn: psycopg.AsyncConnection[Any],
    as_of: dt.date,
    doc_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """시점 필터를 통과한 문단 전체를 읽는다. `doc_ids` 가 있으면 그 문서로 좁힌다."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(_CANDIDATE_SQL, {"as_of": as_of, "doc_ids": doc_ids})
        return list(await cursor.fetchall())


async def _vector_ranking(
    conn: psycopg.AsyncConnection[Any],
    *,
    query: str,
    as_of: dt.date,
    client: EmbeddingClient,
    doc_ids: list[str] | None,
) -> list[tuple[str, float]]:
    """벡터 유사도 순위를 `(문단 id 문자열, 점수)` 로 반환한다."""
    vector = client.embed_query(query)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            _VECTOR_SQL,
            {
                "query": _vector_literal(vector),
                "as_of": as_of,
                "model_id": client.model_id,
                "doc_ids": doc_ids,
            },
        )
        rows = await cursor.fetchall()
    return [(str(row["id"]), float(row["score"])) for row in rows]


def _build_chunks(
    ranked: list[tuple[str, float]],
    by_id: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> tuple[RetrievedChunk, ...]:
    """순위 목록을 `RetrievedChunk` 로 만든다. 중복이 있으면 예외를 던진다."""
    seen: set[str] = set()
    chunks: list[RetrievedChunk] = []
    for rank, (identifier, score) in enumerate(ranked[:limit], start=1):
        if identifier in seen:
            msg = f"검색 결과에 문단 ID 가 중복된다: {identifier}"
            raise SearchError(msg)
        seen.add(identifier)
        row = by_id[identifier]
        chunks.append(
            RetrievedChunk(
                paragraph_id=UUID(identifier),
                doc_id=str(row["doc_id"]),
                doc_version=str(row["version"]),
                article_no=int(row["article_no"]),
                article_title=str(row["article_title"]),
                text_raw=str(row["text_raw"]),
                score=score,
                rank=rank,
            )
        )
    return tuple(chunks)


async def search(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    query: str,
    mode: SearchMode = SearchMode.HYBRID,
    limit: int,
    as_of: dt.date | None = None,
    client: EmbeddingClient | None = None,
    doc_ids: tuple[str, ...] | None = None,
) -> RetrievalResult:
    """질의에 대해 사내 규정 문단 상위 `limit` 건을 찾는다.

    목적:
        세 검색 방식을 같은 계약으로 노출해 골든셋으로 비교할 수 있게 한다.
        4단계 이후에는 이 함수가 그래프 노드의 유일한 검색 창구가 된다.

    구현 이유:
        `mode` 를 인자로 받아 한 함수에 둔다. 방식마다 다른 함수를 노출하면 호출부가
        방식을 고르게 되고, 결합 방식이 확정된 뒤에도 옛 경로가 남는다.

        **기본값은 `HYBRID` 다 (ADR-016).** 골든셋 15건 측정에서 재현율@10 이 벡터
        단독과 **동률**이었고, 사전 고정한 tie-break(DECOY 혼입률@10)가 하이브리드를
        골랐다. 차이가 근소하다는 사실과 반대 증거는 ADR-016 에 적혀 있다 — 이 기본값을
        바꾸려는 사람은 그 절을 먼저 읽어야 한다. `VECTOR`/`LEXICAL` 은 측정 경로로
        남긴다. 지우면 다음 반영에서 같은 비교를 할 수 없다.

        `as_of` 의 기본값을 `None` 으로 두고 함수 안에서 오늘로 채운다. 기본 인자에
        `dt.date.today()` 를 쓰면 모듈 로딩 시각에 고정된다 — 하루 이상 떠 있는
        프로세스에서 조용히 어제를 검색한다.

        **`switches` 를 기본값 없이 받는다 (5단계).** `RETRIEVAL_ENABLED` 가 꺼져 있으면
        여기서 멈춘다 — 빈 결과를 돌려주지 않는다. 빈 결과는 하류에서 "인용할 문단이
        없다"로 읽히고, 그러면 **스위치가 「영향 없음」으로 위장한다.** 기본값을 두지
        않는 이유는 새 호출 경로가 검사를 조용히 건너뛰지 못하게 하기 위해서다 —
        mypy 가 인자 누락을 잡는다.

        검사를 이 함수에 둔 이유는 **여기가 유일한 검색 창구**이기 때문이다. 위임
        승격(`promote.py`)의 재검색도 이 함수를 지난다.

        **`doc_ids` 는 위임 승격이 상위 문서로 범위를 좁혀 재검색할 때만 쓴다**
        (`retrieval/promote.py`, R-22). 기본값 `None` 이면 코퍼스 전체이며, 기존 측정
        경로의 동작은 한 글자도 바뀌지 않는다 — **검색 파라미터(k·모드·임계값)를 고치지
        않는다는 규약**(`docs/10-retrieval-evaluation-protocol.md` §3)을 지키기 위해
        범위 축소를 새 인자로 분리했다. 후보 집합이 줄면 RRF 순위와 BM25 의 IDF 가 함께
        달라지므로, 좁힌 검색의 점수는 전체 검색의 점수와 **비교 가능하지 않다.**
        그 사실이 `RetrievalSource` 로 결과에 표시된다.

    트레이드오프:
        반환값에 후보 집합 크기(`corpus_size`)와 검색 범위(`searched_scope`)를 담아
        결과 객체가 커진다. 그 대신 `INSUFFICIENT_EVIDENCE` 를 만들 때 "어디까지
        찾아봤는가"를 다시 질의하지 않아도 된다 (3단계 §6).

    엣지 케이스:
        모듈 docstring 참조.
        - `RETRIEVAL_ENABLED` 가 꺼짐: `KillSwitchError`. 질의 검증보다 **먼저** 검사한다 —
          꺼진 기능이 입력 형식을 탓하는 오류를 내면 안 된다.
    """
    await switches.require(Switch.RETRIEVAL)
    if not query.strip():
        msg = "빈 질의로 검색할 수 없다"
        raise SearchError(msg)
    if limit <= 0:
        msg = f"limit 은 1 이상이어야 한다: {limit}"
        raise SearchError(msg)

    if doc_ids is not None and not doc_ids:
        msg = "빈 문서 범위로 검색할 수 없다 — 범위를 걸지 않으려면 None 을 넘긴다"
        raise SearchError(msg)

    effective_as_of = as_of or dt.datetime.now(dt.UTC).date()
    scoped = list(doc_ids) if doc_ids is not None else None
    rows = await _candidates(conn, effective_as_of, scoped)
    by_id = {str(row["id"]): row for row in rows}
    scope = tuple(sorted({f"{row['doc_id']} v{row['version']}" for row in rows}))

    if not rows:
        logger.warning("as_of=%s 시점에 검색 대상 문단이 없다", effective_as_of)
        return RetrievalResult(
            mode=mode,
            as_of=effective_as_of,
            chunks=(),
            searched_scope=scope,
            corpus_size=0,
        )

    needs_vector = mode in (SearchMode.VECTOR, SearchMode.HYBRID)
    if needs_vector and client is None:
        msg = f"{mode} 검색에는 임베딩 클라이언트가 필요하다"
        raise SearchError(msg)

    vector_ranked: list[tuple[str, float]] = []
    if needs_vector:
        assert client is not None  # noqa: S101 — 위에서 검사했다. mypy 를 위한 좁히기
        vector_ranked = await _vector_ranking(
            conn, query=query, as_of=effective_as_of, client=client, doc_ids=scoped
        )
        missing = len(rows) - len(vector_ranked)
        if missing:
            logger.warning(
                "임베딩이 없는 문단 %d건이 벡터 검색에서 빠졌다 (model=%s)",
                missing,
                client.model_id,
            )

    lexical_ranked: list[tuple[str, float]] = []
    if mode in (SearchMode.LEXICAL, SearchMode.HYBRID):
        index = lexical.build_index((str(row["id"]), str(row["text_norm"])) for row in rows)
        lexical_ranked = list(lexical.search(index, query, limit=len(rows)))

    if mode is SearchMode.VECTOR:
        ranked = vector_ranked
    elif mode is SearchMode.LEXICAL:
        ranked = lexical_ranked
    else:
        fused = reciprocal_rank_fusion(
            [
                lexical.rank_only(vector_ranked),
                lexical.rank_only(lexical_ranked),
            ],
            limit=limit,
        )
        ranked = list(fused)

    return RetrievalResult(
        mode=mode,
        as_of=effective_as_of,
        chunks=_build_chunks(ranked, by_id, limit=limit),
        searched_scope=scope,
        corpus_size=len(rows),
    )
