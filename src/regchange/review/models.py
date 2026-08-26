"""검토 큐의 값 모델 — 대기 항목, 판단, 반려 사유 코드.

목적:
    검토 화면과 승인 경로가 주고받는 값을 타입이 붙은 불변 값으로 표현한다.

구현 이유:
    **반려 사유를 닫힌 코드로 둔다.** 자유 텍스트만 받으면 집계가 불가능하고, 집계할 수
    없는 반려율은 게이트가 작동하는지 알려주지 않는다 — F-7(승인 게이트가 형식화된다)을
    감시하려면 "무엇 때문에 반려되는가"의 분포를 봐야 한다.

    코드 집합을 **이 시스템이 틀릴 수 있는 방식**에서 도출했다. 기획서 4.5절의 실패 모드와
    1:1 로 대응시킨 것이 아니라, **담당자가 반려 버튼을 누르는 이유**를 나열했다 —
    조항을 잘못 골랐거나(F-6), 놓쳤거나(F-1), 부서·위험도를 잘못 정했거나, 우리 영역이
    아니거나, 근거가 주장을 뒷받침하지 않는 경우다. 앞의 둘과 마지막은 서로 다른 조치로
    이어진다: 잘못 고른 것은 검색·프롬프트, 놓친 것은 재현율, 근거 부족은 gate 3단이다.

    `OTHER` 는 자유 기술을 강제한다(DB CHECK). 강제하지 않으면 `OTHER` 가 기본값이 되고,
    그러면 코드 집합이 있어도 없는 것과 같다. **`OTHER` 가 쌓이는 것이 코드를 늘릴 신호다.**

트레이드오프:
    - 코드가 7개다. 더 잘게 나누면 담당자가 고르기 어렵고, 더 뭉치면 조치가 갈리지 않는다.
      **조치가 갈리는 단위**를 기준으로 잘랐다.
    - 수정 승인(`EDIT`)의 수정 내용을 자유 형식 사전으로 받는다. 구조를 강제하면 화면이
      먼저 정해져야 하는데, 지금은 무엇을 고치는지 관측된 바가 없다. 6단계에서 쌓인
      `edit_json` 을 보고 구조를 정한다.

엣지 케이스:
    - **`ACCEPT` 인데 사유 코드가 있음**: 허용한다. 수락하면서 메모를 남기는 것은 자연스럽다.
    - **`REJECT` 인데 사유 코드가 없음**: DB CHECK 가 거부한다. 여기서도 검사하지만
      **DB 가 단일 진실**이다 — 두 곳에 제약을 두면 한쪽이 갱신되지 않았을 때 어긋난다.
    - **기한이 없는 항목**: `due_at` 이 None 이며 `overdue` 는 False 다. 기한을 모르는 것과
      기한이 남은 것을 같은 값으로 두지 않기 위해 `due_known` 을 따로 노출한다.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Frozen(BaseModel):
    """불변 모델 공통 설정."""

    model_config = ConfigDict(frozen=True)


class ReviewDecisionKind(StrEnum):
    """검토자의 판단 3종. `review_decision.decision` CHECK 와 같은 값이어야 한다."""

    ACCEPT = "ACCEPT"
    EDIT = "EDIT"
    REJECT = "REJECT"


class ReviewState(StrEnum):
    """평가의 사람 처리 상태. `impact_assessment.review_state` CHECK 와 같은 값이다."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    NOT_QUEUED = "NOT_QUEUED"
    """근거 부족으로 큐에 넣지 않은 건. 사람이 처리할 대상이 아니다."""


class ReasonCode(StrEnum):
    """반려·수정 사유. **조치가 갈리는 단위**로 나눴다."""

    WRONG_PARAGRAPH = "WRONG_PARAGRAPH"
    """지목한 사내 조항이 틀렸다. 그럴듯하게 틀린 경우이며 F-6 이다."""
    MISSED_PARAGRAPH = "MISSED_PARAGRAPH"
    """걸리는 조항을 놓쳤다. 재현율 문제이며 F-1 이다."""
    WRONG_DEPARTMENT = "WRONG_DEPARTMENT"
    """부서 배정이 틀렸다. 근거 표현의 해석 문제다."""
    WRONG_RISK = "WRONG_RISK"
    """위험도가 틀렸다."""
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """우리 영역이 아니다. 이관 대상이다."""
    INSUFFICIENT_BASIS = "INSUFFICIENT_BASIS"
    """인용은 실재하지만 주장을 뒷받침하지 않는다. **gate 3단이 놓친 것**이다."""
    OTHER = "OTHER"
    """위에 없다. 자유 기술이 필수이며, 쌓이면 코드를 늘린다."""


class DecisionRequest(Frozen):
    """검토자가 보내는 판단 하나.

    `reviewed_ms` 를 요청이 들고 오는 이유는 **화면에서 실제로 걸린 시간**이기 때문이다.
    서버에서 큐 진입 시각과의 차이로 계산하면 "화면을 열어 둔 채 퇴근한 시간"이 검토
    시간으로 잡힌다. F-7 을 감시하려는 값은 후자가 아니다.
    """

    decision: ReviewDecisionKind
    decided_by: str
    reason_code: ReasonCode | None = None
    reason_note: str | None = None
    edit: dict[str, Any] | None = None
    reviewed_ms: int = 0


class ReviewItem(Frozen):
    """검토 대기 목록·상세가 보는 평가 한 건."""

    id: str
    thread_id: str
    created_at: dt.datetime
    law_name: str
    article_path: str
    revision_kind: str
    change_type: str
    status: str
    """gate 가 정한 기계의 판정. 검토자가 바꿀 수 없는 값이다."""
    review_state: ReviewState
    obligation_type: str
    risk_level: str
    confidence: str
    summary: str
    reason: str
    revisions: int
    draft: dict[str, Any]
    grounding: dict[str, Any]
    discarded: list[dict[str, Any]]
    queued_at: dt.datetime | None
    due_at: dt.datetime | None

    @property
    def due_known(self) -> bool:
        """기한을 알고 있는가. 모르는 것과 남아 있는 것은 다른 사실이다."""
        return self.due_at is not None

    def overdue(self, *, now: dt.datetime) -> bool:
        """기한을 넘겼는가. 기한을 모르면 False 이며 그 사실은 `due_known` 이 말한다."""
        return self.due_at is not None and now > self.due_at
