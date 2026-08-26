"""검토 큐 — 영향평가 적재, 대기 목록 질의, 승인·반려 기록 (원칙 4).

목적:
    사람이 판단해야 할 영향평가를 큐에 넣고, 대기 목록과 기한 초과를 질의하며, 판단의
    결과를 승인 레코드로 남긴다. 그래프·API·운영 명령이 이 패키지 하나를 공유한다.

구현 이유:
    **별도 패키지로 나눈 이유는 세 곳이 같은 질의를 필요로 하기 때문이다.** 그래프 노드는
    초안을 적재하고 승인을 기록하며, API 는 대기 목록과 상세를 읽고, `ops alerts` 는 기한
    초과를 센다. 이 질의가 세 곳에 흩어지면 "대기 중"의 정의가 세 개가 되고, 그 셋은
    어긋난다 — `ops` 를 별도 패키지로 뺀 것과 같은 판단이다 (ADR-014).

    **이 패키지는 `prompts` 를 import 하지 않는다.** 초안을 `ImpactDraft` 가 아니라
    사전(JSON)으로 다룬다. 검토 큐가 프롬프트 스키마를 알기 시작하면 초안 스키마가 바뀔
    때마다 큐가 함께 움직이고, 더 중요하게는 **`dispatch` 가 나중에 이 패키지를 쓰게 될 때
    프롬프트를 간접 import 하게 된다** — 원칙 5 가 금지하는 방향이다.

    **승인 레코드가 정본이고 `review_state` 는 파생이다.** 큐 질의를 위해 현재 상태를
    컬럼으로 들고 있지만, "무엇이 언제 왜 결정됐는가"의 답은 `review_decision` 행들이다.
    두 값이 어긋나면 행이 맞다.

트레이드오프:
    - 승인 기록과 발송 대상 생성이 한 트랜잭션이다. 나누면 "승인은 됐는데 발송 대상이
      없는" 상태가 생기고, 그 상태는 아무 오류도 내지 않으면서 담당자의 승인을 무효로
      만든다. 원자성을 얻고 트랜잭션 범위를 넓히는 것을 감수했다.
    - 대기 목록 질의가 `draft_json` 을 통째로 읽는다. 목록 화면에는 과한 양이지만,
      요약만 읽으면 화면이 상세를 다시 질의해야 하고 그 사이에 상태가 바뀔 수 있다.

엣지 케이스:
    - **큐에 넣지 않은 평가**(`INSUFFICIENT_EVIDENCE`): 행은 남기고 `queued_at` 을 NULL 로
      둔다. 이관 비율(ADR-013 신호 4번)을 세려면 그 행이 있어야 한다.
    - **기한을 모르는 대기**: `due_at` 이 NULL 이다. 기한 초과로 세지 않고 **따로 센다** —
      임의의 기본 기한으로 채우면 근거 없는 숫자가 운영 지표가 된다.
    - **이미 결정된 건에 다시 결정이 들어옴**: `review_state` 가 `PENDING` 이 아니면
      거부한다. 두 번째 승인이 outbox 행을 하나 더 만들면 발송이 두 번 일어난다.
    - **스모크 실행이 남긴 행**: 지우지 않고 **세지 않는다** (`SMOKE_REVIEWERS`,
      `SMOKE_ASSESSMENT_IDS`). 불변 테이블에서 지우려면 `TRUNCATE` 가 필요하고 그것은
      이 저장소가 사건으로 기록한 행위다.
"""

from regchange.review.models import (
    DecisionRequest,
    ReasonCode,
    ReviewDecisionKind,
    ReviewItem,
    ReviewState,
)
from regchange.review.queue import (
    SMOKE_ASSESSMENT_IDS,
    SMOKE_REVIEWERS,
    ReviewError,
    count_overdue,
    insert_assessment,
    list_pending,
    load_item,
    record_decision,
    summarize_assessments,
    summarize_reviews,
)

__all__ = [
    "SMOKE_ASSESSMENT_IDS",
    "SMOKE_REVIEWERS",
    "DecisionRequest",
    "ReasonCode",
    "ReviewDecisionKind",
    "ReviewError",
    "ReviewItem",
    "ReviewState",
    "count_overdue",
    "insert_assessment",
    "list_pending",
    "load_item",
    "record_decision",
    "summarize_assessments",
    "summarize_reviews",
]
