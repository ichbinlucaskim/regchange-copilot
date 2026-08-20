"""운영 경계 — 매일 도는 일과 그 실행 이력.

목적:
    cron 이 부르는 일일 작업(`daily`), 그 결과의 기록(`record`), 그리고 "언제부터
    돌았고 며칠 실패했나"에 답하는 조회(`history`)를 담는다.

구현 이유:
    `pipeline` 과 나눈 이유가 이 패키지의 존재 이유다. `pipeline` 은 **한 건을
    어떻게 처리하는가**(MST 하나 → change_set 하나)를 조립하고, `ops` 는 **매일
    무엇을 얼마나 도는가**를 정한다. 스케줄·재확인 창·실패 격리·실행 이력은
    전부 후자에 속하며, 전자에 섞으면 "이 함수는 한 건짜리인가 하루치인가"가
    호출부마다 달라진다.

    **운영 실적은 압축할 수 없는 유일한 자산이다.** 코드는 나중에 다시 쓸 수
    있지만 "N개월간 매일 돌았다"는 만들 수 없다. 그래서 실행 이력을 로그가 아니라
    테이블에 남기고, 그 테이블을 읽는 조회를 코드로 고정한다 — 같은 질문에 항상
    같은 답이 나와야 그것이 근거가 된다.

    **실패를 숨기지 않는다.** "92일 중 89일 성공, 3일 실패(API 지연 2건, 카나리아
    실패 1건)"가 "완벽하게 돌았습니다"보다 신뢰가 간다. 후자는 아무도 믿지 않는다.
    그래서 재시도를 늘려 실패를 없애는 대신 실패를 값으로 남긴다.

트레이드오프:
    이 패키지는 DB 와 외부 API 를 모두 안다. 순수하지 않으며 그래야 한다 —
    조립하는 곳이 양쪽을 알지 못하면 조립을 호출부가 하게 되고, 그 호출부가
    cron 셸 스크립트가 된다. 셸에 들어간 로직은 테스트되지 않는다.

엣지 케이스:
    - 노트북이 꺼져 있어 실행되지 않은 날: 기록이 없다. 그 부재를 `ops summary` 가
      미실행 일수로 계산해 보여준다 — 부재는 기록할 수 없으므로 달력에서 뺀다.
    - 프로세스가 예외로 죽은 경우: 호출부가 실패 행을 남긴다. "실패한 날"과
      "실행하지 않은 날"은 다른 상태이며 조치도 다르다.
"""

from regchange.ops.daily import already_processed, extract_detected, run_daily
from regchange.ops.history import (
    Alert,
    AlertKind,
    HistoryRow,
    OpsSummary,
    fetch_alerts,
    fetch_history,
    fetch_summary,
)
from regchange.ops.models import (
    DEFAULT_LOOKBACK_DAYS,
    DailyRunResult,
    LawOutcome,
    LawOutcomeStatus,
    OpsRunStatus,
    derive_status,
    lookback_dates,
)
from regchange.ops.record import record_failure, record_run

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "Alert",
    "AlertKind",
    "DailyRunResult",
    "HistoryRow",
    "LawOutcome",
    "LawOutcomeStatus",
    "OpsRunStatus",
    "OpsSummary",
    "already_processed",
    "derive_status",
    "extract_detected",
    "fetch_alerts",
    "fetch_history",
    "fetch_summary",
    "lookback_dates",
    "record_failure",
    "record_run",
    "run_daily",
]
