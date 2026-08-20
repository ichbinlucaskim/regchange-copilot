"""조문 단위 차분을 결정론적으로 계산하는 패키지 (원칙 1).

목적:
    법령 개정 전후의 조문을 비교해 변경 유형(신설/개정/삭제/조번호이동/편집상
    변경)과 변경된 텍스트 범위를 산출한다. LLM 은 이 패키지의 출력을 입력으로
    받아 "그 변화가 무엇을 의미하는가"만 해석한다.

구현 이유:
    "무엇이 바뀌었는가"는 사실 확인이지 추론이 아니다. 원문 두 버전이 모두 있는
    상황에서 LLM 을 쓰면 정확도는 떨어지고 비용과 비결정성만 늘어난다. 순수
    함수로 작성해 같은 입력에 항상 같은 출력을 보장한다.

트레이드오프:
    텍스트 유사도 기반이므로 조문이 전면 재작성된 경우 의미적으로는 "개정"인
    변경이 "삭제 + 신설"로 보고될 수 있다. 의미적 연속성 판단을 포기한 대신
    결정론성과 재현성을 얻었다. 재현성이 감사 요구사항이므로 이 교환은
    의도적이다. 연속성 해석이 필요하면 그 판단은 LLM 단계로 넘기되, diff 자체는
    바꾸지 않는다.

엣지 케이스:
    - I/O 를 수행하지 않는다. 이 패키지는 네트워크·DB·파일에 접근하지 않으며,
      따라서 실행 환경 없이 단위 테스트로 전부 고정할 수 있다.
    - 공백·줄바꿈만 다른 경우: 정규화 후 비교하여 편집상 변경으로 분류한다.
    - 빈 문자열 입력: 예외를 던진다. 파서 버그를 정상 diff 로 위장하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from regchange.diff.compare import classify, diff_articles, index_by_ref, priority_rank_for
from regchange.diff.models import (
    EDITORIAL_PRIORITY_RANK,
    EXPLICIT_SCORE,
    TITLE_SCORE,
    ArticleChange,
    ArticleSnapshot,
    Cardinality,
    ChangeType,
    DiffCounts,
    DiffError,
    DiffResult,
    EvidenceKind,
    MoveCandidate,
    MoveMetrics,
    MoveWindow,
)
from regchange.diff.moves import (
    CANDIDATE_POOL_WARN_SIZE,
    build_move_candidates,
    explicit_edges,
)
from regchange.diff.snapshots import snapshot_from_unit

__all__ = [
    "CANDIDATE_POOL_WARN_SIZE",
    "EDITORIAL_PRIORITY_RANK",
    "EXPLICIT_SCORE",
    "TITLE_SCORE",
    "ArticleChange",
    "ArticleSnapshot",
    "Cardinality",
    "ChangeType",
    "DiffCounts",
    "DiffError",
    "DiffResult",
    "EvidenceKind",
    "MoveCandidate",
    "MoveMetrics",
    "MoveWindow",
    "build_move_candidates",
    "classify",
    "diff_articles",
    "diff_versions",
    "explicit_edges",
    "index_by_ref",
    "priority_rank_for",
    "snapshot_from_unit",
]


def diff_versions(
    from_articles: Iterable[ArticleSnapshot],
    to_articles: Iterable[ArticleSnapshot],
    *,
    window: MoveWindow,
) -> DiffResult:
    """두 버전을 비교해 변경과 이동 후보를 한 번에 낸다.

    목적:
        `change_set` 하나가 담을 내용을 전부 만든다. 적재 계층은 이 결과를 그대로
        쓰기만 한다.

    구현 이유:
        변경 판정과 이동 후보 생성을 한 진입점으로 묶는다. 둘을 따로 부르게 하면
        호출자마다 순서와 조합이 달라지고, 그중 하나가 이동 후보를 빠뜨려도
        아무 오류가 나지 않는다.

        **두 결과를 합쳐서 확정하지 않는다.** 블록 재번호에서 같은 번호의 다른
        조문이 MODIFIED 로 잡히고 동시에 이동 후보가 생기는데, 그 둘을 합쳐
        "이동이므로 MODIFIED 를 취소한다"고 판단하지 않는다. 그 판단이 곧 자동
        확정이며 ADR-003 이 금지한 것이다. 두 신호를 나란히 제시하고 해석은
        검토자에게 맡긴다.

    트레이드오프:
        입력을 두 번 순회한다(변경 판정 1회, 후보 생성 1회). 조문 수가 최대
        682건이므로 비용이 없고, 두 계산이 서로를 오염시키지 않는다.

    엣지 케이스:
        - 후보 풀이 `CANDIDATE_POOL_WARN_SIZE` 를 넘음: 결과에 크기가 남으므로
          호출자가 경고를 낼 수 있다. 여기서 잘라내지 않는다 — 조용한 절단은
          이 저장소가 반복해서 막아 온 형태다.
        - 이동 후보 0건: "이동 없음"이 아니라 "판정 불가"일 수 있다. 풀 크기와
          창 밖 표기 건수를 함께 보면 어느 쪽인지 구별된다.
    """
    from_list = list(from_articles)
    to_list = list(to_articles)

    changes, counts = diff_articles(from_list, to_list)
    candidates, metrics = build_move_candidates(from_list, to_list, window=window)

    return DiffResult(
        counts=counts,
        changes=changes,
        candidates=candidates,
        moves_in_window=metrics.moves_in_window,
        moves_out_of_window=metrics.moves_out_of_window,
        out_of_window_dates=metrics.out_of_window_dates,
        candidate_pool_size=metrics.candidate_pool_size,
        empty_body_refs=metrics.empty_body_refs,
        explicit_edge_count=metrics.explicit_edge_count,
        unresolved_explicit_edges=metrics.unresolved_explicit_edges,
    )
