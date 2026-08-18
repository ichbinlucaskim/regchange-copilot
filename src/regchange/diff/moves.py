"""이동 후보 생성 — 명시 표기 우선, 제목·유사도는 fallback (ADR-003).

목적:
    조문 이동 후보를 만들고 세 신호(EXPLICIT / TITLE / SIMILARITY)를 함께 남긴다.
    **확정하지 않는다.** 만들어지는 것은 후보까지다.

구현 이유:
    **후보 풀을 `DELETED 와 명시 이동의 출발`, `ADDED 와 명시 이동의 도착`의 곱 으로
    잡는다.** ADR-003 의 결정 다이어그램은 "삭제 후보 곱하기 신설 후보"라고 적었는데,
    실측에서 그 풀이 공집합이 되는 경우가 확인됐다 — 특금법 2011↔2020 은 이동이
    전부 `제6조→제9조` 식 블록 재번호라 **DELETED 가 0건**이고, 명시 표기 15건이
    있는데도 TITLE·SIMILARITY 가 한 번도 계산되지 않았다.

    그러면 "세 신호를 함께 남긴다"는 ADR-003 의 설계가 실현되지 않는다. 세 신호가
    엇갈리는 경우를 검토자가 봐야 파서 버그를 잡을 수 있는데, 신호가 하나뿐이면
    엇갈릴 수가 없다. 그래서 명시 표기가 가리키는 조문을 풀에 넣어 **같은 쌍에
    대해 세 신호를 모두 계산한다.**

    **날짜 창으로 거른다.** `조문참고자료` 는 그 조문의 이동 이력 전체를 누적한다 —
    실측에서 표기 128건 중 문서 시행 연도와 같은 해가 0건이고, 소득세법 한 문서가
    2006·2010·2018·2022 네 시점을 함께 담는다. 거르지 않으면 2008년 이동이 오늘의
    후보로 검토 큐에 올라간다.

    **거른 건수를 남긴다.** 필터가 조용히 버리는 경로가 되면 "몇 건을 왜 제외했나"에
    답할 수 없고, 필터가 틀렸을 때 발견되지 않는다.

    유사도는 표준 라이브러리(`difflib`)를 쓴다. 정교한 알고리즘이 필요한 자리가
    아니다 — 확정은 사람이 하고, 이 점수는 검토자에게 제시되는 신호일 뿐이다.

트레이드오프:
    **점수 임계값을 두지 않는다.** 풀 안의 모든 쌍을 후보로 낸다. 임계값을 두면 그
    값이 곧 튜닝 대상이 되는데, 근거 데이터가 특금법 한 쌍뿐이라 지금 정하면 한
    사례에 과적합된다(ADR-003, adr-011 예약). 대신 풀 크기를 결과에 남겨 대형
    법령에서 폭발하는지 감시한다.

    명시 표기가 **없는** 이동은 이 풀로 잡히지 않는다. 블록 재번호는 DELETED 도
    ADDED 도 만들지 않기 때문이다. ADR-003 근거 (c)("명시 표기가 없는 이동이 존재할
    수 있다")는 이 구현으로 검증되지 않으며, 그 사실을 ADR 에 기록한다.

엣지 케이스:
    - 본문이 빈 조문(4개 법령 51건): 유사도가 무의미하므로 유사도 계산에서 제외하고
      좌표를 `empty_body_refs` 에 남긴다. 조용히 0점 처리하지 않는다.
    - 제목이 없는 조문(특금법 34개 중 7개): TITLE 신호를 쓸 수 없다. `evidence` 에
      `title_available: false` 로 남겨 "일치하지 않음"과 구별한다.
    - 날짜가 없는 이동 표기: 창 밖으로 다룬다. 실측 128건은 전부 날짜를 갖는다.
    - 자기 자신으로의 이동: 만들지 않는다. 스키마 CHECK 도 같은 것을 막는다.
    - 같은 쌍에 EXPLICIT 과 TITLE 이 모두 성립: `evidence_kind` 는 EXPLICIT 이고
      `evidence` 에 셋을 모두 남긴다.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Iterable

from regchange.diff.models import (
    EXPLICIT_SCORE,
    TITLE_SCORE,
    ArticleSnapshot,
    Cardinality,
    EvidenceKind,
    MoveCandidate,
    MoveMetrics,
    MoveWindow,
)
from regchange.parse.models import MoveKind

CANDIDATE_POOL_WARN_SIZE = 2000
"""후보 풀이 이 크기를 넘으면 호출자가 경고를 남긴다.

근거: 자본시장법 682조문이 관측 최대이고, 그 법령에서 삭제·신설이 각 45건씩
발생하면 2,025쌍이 된다. 그 규모를 넘으면 검토 큐가 소음으로 채워질 위험이
ADR-003 의 "틀렸음을 알게 되는 신호 1번"에 해당하므로 눈에 띄어야 한다.
정확한 값보다 **감시가 존재한다는 것**이 요점이며, 실측이 쌓이면 조정한다.
"""


def explicit_edges(
    to_articles: Iterable[ArticleSnapshot],
    window: MoveWindow,
) -> tuple[dict[tuple[tuple[int, int], tuple[int, int]], str], int, int, tuple[str, ...]]:
    """도착 버전의 명시 이동 표기를 창으로 걸러 간선으로 만든다.

    반환: (간선 → 파싱 원문, 창 안 건수, 창 밖 건수, 창 밖 날짜들)

    `MOVED_FROM` 은 "이 조문은 제N조에서 왔다"이므로 간선 `제N조 → 이 조문`이고,
    `PREVIOUS_MOVED_TO` 는 "종전 제N조는 제M조로 이동"이므로 간선 `제N조 → 제M조`다.
    두 서술은 같은 이동을 양쪽에서 적은 것처럼 보이지만 **실측에서 일치하지 않는다** —
    68건과 58건의 교집합이 50건이며, 12건은 종전 번호가 비어 있어 출발지 기준
    서술을 적을 host 조문 자체가 없다 (ADR-003 근거 b).

    `PREVIOUS_DELETED` 는 이동이 아니므로 간선을 만들지 않는다.
    """
    edges: dict[tuple[tuple[int, int], tuple[int, int]], str] = {}
    in_window = out_of_window = 0
    out_dates: list[str] = []

    for article in to_articles:
        for move in article.moves:
            when = move.dates[0] if move.dates else None
            if move.kind is MoveKind.MOVED_FROM and move.source is not None:
                edge = ((move.source.article_no, move.source.branch_no), article.ref)
            elif (
                move.kind is MoveKind.PREVIOUS_MOVED_TO
                and move.source is not None
                and move.target is not None
            ):
                edge = (
                    (move.source.article_no, move.source.branch_no),
                    (move.target.article_no, move.target.branch_no),
                )
            else:
                continue

            if window.contains(when):
                in_window += 1
                if edge[0] != edge[1]:
                    edges[edge] = move.raw
            else:
                out_of_window += 1
                if when is not None:
                    out_dates.append(when.isoformat())

    return edges, in_window, out_of_window, tuple(sorted(set(out_dates)))


def _similarity(left: ArticleSnapshot, right: ArticleSnapshot) -> float | None:
    """두 조문의 조립본 유사도. 어느 쪽이든 비어 있으면 None(계산 불가)이다."""
    if left.is_empty or right.is_empty:
        return None
    return difflib.SequenceMatcher(None, left.body_norm, right.body_norm).ratio()


def _cardinality(
    edge: tuple[tuple[int, int], tuple[int, int]],
    out_degree: dict[tuple[int, int], int],
    in_degree: dict[tuple[int, int], int],
) -> Cardinality:
    """같은 근거 종류의 후보들만 보고 관계를 정한다. 1:1 이 아닌 것을 줄이지 않는다.

    **근거 종류별로 나눠 세는 이유**: 유사도 후보는 풀 안의 모든 쌍에 대해 만들어지므로
    (임계값을 두지 않기로 했다) 전체를 한 그래프로 보면 차수가 항상 높아 모든 후보가
    N:M 이 된다. 그러면 cardinality 가 아무것도 구별하지 못한다.

    나눠 세면 각 신호가 말하는 관계가 그대로 드러난다 — 특금법 2011↔2020 에서
    EXPLICIT 은 전부 1:1 이고, 제목 `벌칙` 이 양쪽 2건씩이라 TITLE 은 N:M 이다.
    **ADR-003 이 기록한 2 대 2 모호성이 TITLE 계층에 그대로 남고, EXPLICIT 이 그것을
    1:1 로 푸는 모습**이 보인다. 신호가 엇갈리는지 보라는 ADR-003 의 요구가 여기서
    실현된다.
    """
    many_targets = out_degree[edge[0]] > 1
    many_sources = in_degree[edge[1]] > 1
    if many_targets and many_sources:
        return Cardinality.MANY_TO_MANY
    if many_targets:
        return Cardinality.ONE_TO_MANY
    if many_sources:
        return Cardinality.MANY_TO_ONE
    return Cardinality.ONE_TO_ONE


def build_move_candidates(
    from_articles: Iterable[ArticleSnapshot],
    to_articles: Iterable[ArticleSnapshot],
    *,
    window: MoveWindow,
) -> tuple[tuple[MoveCandidate, ...], MoveMetrics]:
    """이동 후보를 만들고 관측 지표를 함께 돌려준다.

    목적:
        검토자가 판단할 후보를 만든다. **확정하지 않는다** (ADR-003).

    구현 이유:
        모든 후보에 세 신호를 계산해 `evidence` 에 담는다. 세 신호가 엇갈리는 것이
        파서 버그의 신호이기 때문이다 — 명시 표기가 가리키는 조문과 유사도가
        가리키는 조문이 다르면 둘 중 하나가 틀렸다는 뜻이고, 검토자가 그것을 볼 수
        있어야 한다.

        `cardinality` 는 후보를 다 만든 뒤 집합 전체를 보고 정한다. 쌍마다 즉시
        정하면 나중에 추가되는 후보를 반영하지 못해 1:N 이 1:1 로 기록된다.

    트레이드오프:
        풀 안의 모든 쌍에 유사도를 계산한다. 풀 크기가 n 곱하기 m 이므로 대형 법령에서
        비용이 오른다. 임계값으로 미리 자르지 않는 이유는 위 모듈 docstring 참조.

    엣지 케이스:
        - 창 안 명시 표기가 0건이고 DELETED/ADDED 도 0건: 후보 0건. "이동 없음"이
          아니라 **"판정 불가"**로 읽어야 하며, 호출자가 풀 크기 0 을 함께 기록한다.
        - 명시 표기의 출발/도착이 두 버전 어디에도 없는 좌표: 후보를 만들 수 없다.
          간선은 남지만 스냅샷이 없으므로 건너뛴다.
    """
    before = {a.ref: a for a in from_articles}
    after = {a.ref: a for a in to_articles}

    edges, in_window, out_of_window, out_dates = explicit_edges(after.values(), window)

    sources = (set(before) - set(after)) | {edge[0] for edge in edges}
    targets = (set(after) - set(before)) | {edge[1] for edge in edges}
    pool = [
        (s, t)
        for s in sorted(sources)
        for t in sorted(targets)
        if s in before and t in after and s != t
    ]

    unresolved = tuple(
        edge for edge in sorted(edges) if edge[0] not in before or edge[1] not in after
    )

    empty_refs: set[tuple[int, int]] = set()
    scored: list[tuple[tuple[int, int], tuple[int, int], float, EvidenceKind, dict[str, object]]]
    scored = []

    for src, dst in pool:
        source, target = before[src], after[dst]
        if source.is_empty:
            empty_refs.add(src)
        if target.is_empty:
            empty_refs.add(dst)

        raw = edges.get((src, dst))
        title_available = source.title is not None and target.title is not None
        title_match = title_available and source.title == target.title
        similarity = _similarity(source, target)

        evidence: dict[str, object] = {
            "explicit": raw is not None,
            "explicit_raw": raw,
            "title_available": title_available,
            "title_match": title_match,
            "from_title": source.title,
            "to_title": target.title,
            "similarity": similarity,
        }

        if raw is not None:
            kind, score = EvidenceKind.EXPLICIT, EXPLICIT_SCORE
        elif title_match:
            kind, score = EvidenceKind.TITLE, TITLE_SCORE
        elif similarity is not None:
            kind, score = EvidenceKind.SIMILARITY, similarity
        else:
            # 본문이 비어 유사도를 낼 수 없고 다른 신호도 없다. 후보로 만들지 않되
            # 좌표는 `empty_refs` 에 남아 사후에 확인할 수 있다.
            continue

        scored.append((src, dst, score, kind, evidence))

    out_degree: dict[EvidenceKind, dict[tuple[int, int], int]] = defaultdict(
        lambda: defaultdict(int)
    )
    in_degree: dict[EvidenceKind, dict[tuple[int, int], int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for src, dst, _score, kind, _evidence in scored:
        out_degree[kind][src] += 1
        in_degree[kind][dst] += 1

    candidates = tuple(
        MoveCandidate(
            from_ref=src,
            to_ref=dst,
            score=score,
            evidence_kind=kind,
            evidence=evidence,
            cardinality=_cardinality((src, dst), out_degree[kind], in_degree[kind]),
        )
        for src, dst, score, kind, evidence in scored
    )

    metrics = MoveMetrics(
        moves_in_window=in_window,
        moves_out_of_window=out_of_window,
        out_of_window_dates=out_dates,
        candidate_pool_size=len(pool),
        explicit_edge_count=len(edges),
        empty_body_refs=tuple(sorted(empty_refs)),
        unresolved_explicit_edges=unresolved,
    )
    return candidates, metrics
