"""일일 운영 실행의 값 — 상태, 처분, 날짜 창. I/O 없이 판정되는 것만 둔다.

목적:
    "이 실행은 성공인가"와 "어느 날짜를 폴링할 것인가"를 순수 함수로 고정한다.

구현 이유:
    실행 상태 판정은 운영 실적 주장의 근거다 — "92일 중 89일 성공"의 89가 여기서
    나온다. 그 판정이 DB 접속과 네트워크에 얽혀 있으면 테스트가 통합 테스트가 되고,
    통합 테스트는 경계 조건을 다 돌지 않는다. **부분 성공·전량 실패·미수행의 구별은
    경계 조건 그 자체**이므로 순수 함수로 떼어 놓는다.

트레이드오프:
    실행 결과 dataclass가 커진다(필드 15개 이상). 작은 값으로 쪼개면 호출부가
    조립을 하게 되고, 조립하는 곳마다 빠뜨리는 필드가 생긴다. 한 덩어리로 두고
    DB 기록을 한 함수가 하는 쪽을 택했다.

엣지 케이스:
    - 카나리아 실패: 실행 상태는 `SKIPPED_CANARY_FAILED`이며 **실패가 아니라
      미수행**이다. 실패율에 넣으면 우리 성능이 아닌 것을 우리 성능으로 계상한다.
    - 폴링한 날짜가 전부 실패: `FAILED`. 일부만 실패면 `PARTIAL`이다.
    - 코퍼스 대상 개정이 0건: `SUCCEEDED_ZERO`. 12개월 실측에서 코퍼스 개정일이
      14일/365일이므로 **0건이 기본 상태**이며 실패가 아니다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from regchange.ingest.canary import CanaryResult, IngestRunStatus

DEFAULT_LOOKBACK_DAYS = 7
"""최근 며칠을 재확인하는가.

**두 가지를 동시에 커버한다.**

1. **놓친 날 따라잡기.** 노트북이 꺼져 있거나 슬립이면 그날 실행이 없다. 주말 +
   연휴가 겹치면 연속 미실행이 3~5일까지 간다(설·추석 연휴). 7일이면 한 주가
   통째로 비어도 다음 실행이 전부 회수한다.
2. **법제처 등재 지연 흡수.** `regDt`는 공포일자인데 법제처 DB 등재가 언제
   이루어지는지 **우리는 측정한 적이 없다** — `LS_HISTORY` docstring이 그 지연을
   "우리가 통제할 수 없는 인지 지연의 하한선"이라 부르고 미확인으로 남겨 두었다.
   측정값이 없으므로 하루를 가정하지 않고 여유를 둔다.

**비용이 작아서 크게 잡을 수 있다.** 재확인 1일치는 폴링 1회(0건이면 재요청까지
2회)이고, 이미 처리한 MST는 `SKIPPED_DONE`으로 끝나 본문 수집이 일어나지 않는다.
7일 창의 하루 비용은 카나리아 1회 + 폴링 7회 ≈ 8회이며 호출 간격 1.2초를 곱해도
10초다.

**더 크게 잡지 않는 이유**는 재확인이 무한정 늘면 "놓친 날"이 언제까지 회수
가능한지가 흐려지기 때문이다. 7일을 넘겨 비면 그것은 회수 대상이 아니라 **미실행
기록**이어야 하고, `ops summary`가 그것을 미실행 일수로 보고한다. 장기 부재
(휴가 등)에는 `--days`로 명시적으로 늘린다 — 명시적 행위로 남는 편이 낫다.
"""

OPS_CALENDAR_OFFSET = dt.timedelta(hours=9)
"""운영 달력의 UTC 오프셋(KST).

**기록은 전부 UTC이고 표시만 KST로 바꾼다** (`new_run_id` docstring의 규칙).
그런데 "며칠 돌았나"는 UTC로 세면 틀린다 — 07:00 KST 실행은 UTC로 전날 22:00이라
날짜가 하루 밀린다. 운영자가 검증할 수 있는 달력은 자기가 사는 달력이다.

`ZoneInfo("Asia/Seoul")`이 아니라 **고정 오프셋**을 쓴다. 대한민국은 1988년
서울올림픽 이후 서머타임을 시행하지 않으므로 KST는 UTC+9 고정이며, 고정 오프셋이면
tzdata 패키지가 없는 실행 환경(슬림 컨테이너)에서도 같은 값이 나온다. 이 값이
틀리게 되는 유일한 경우는 대한민국이 서머타임을 재도입하는 것이고, 그때는 이 상수와
SQL 양쪽을 함께 고친다 — 그래서 한 곳에 이름을 붙여 두었다.
"""


class OpsRunStatus(StrEnum):
    """일일 실행 하나의 상태 — "실패"와 "안 함"과 "볼 것이 없었음"을 구별한다.

    목적:
        운영 실적 집계의 분류축. `ops summary`의 성공/실패/미실행 일수가 이 값에서
        나온다.

    구현 이유:
        `IngestRunStatus`(수집 1회의 상태)를 재사용하지 않았다. 실행 하나가 날짜
        여러 개를 폴링하고 법령 여러 개를 처리하므로 **부분 성공이 존재**하는데,
        수집 1회에는 그 상태가 없다. 같은 enum에 `PARTIAL`을 넣으면 수집 함수가
        낼 수 없는 값이 그 타입에 생긴다.

    트레이드오프:
        비슷한 이름의 enum이 둘이 되어 혼동할 수 있다. 대신 층위가 분리된다 —
        `IngestRunStatus`는 "이 날짜 요청이 어땠나", `OpsRunStatus`는 "오늘 실행이
        어땠나"다.

    엣지 케이스:
        - `SKIPPED_CANARY_FAILED`는 실패율에서 제외한다. 우리 파이프라인이 아니라
          외부 API 상태에 대한 판정이다.
        - `SUCCEEDED_ZERO`가 오래 이어지는 것은 정상일 수 있다. 임계는
          `CONSECUTIVE_ZERO_ALERT_DAYS` 참조.
    """

    SUCCEEDED = "SUCCEEDED"
    """새로 처리한 법령이 있고 실패가 없다."""

    SUCCEEDED_ZERO = "SUCCEEDED_ZERO"
    """정상 실행인데 코퍼스 대상 새 개정이 0건이다. 연속 0건 알람의 계수 대상이다."""

    PARTIAL = "PARTIAL"
    """일부 날짜 또는 일부 법령이 실패했다. 나머지는 처리됐다."""

    FAILED = "FAILED"
    """폴링한 날짜가 전부 실패했다."""

    SKIPPED_CANARY_FAILED = "SKIPPED_CANARY_FAILED"
    """카나리아 실패로 수집하지 않았다. **미수행이며 실패가 아니다.**"""


class LawOutcomeStatus(StrEnum):
    """법령 버전(MST) 하나의 처분. 상호 배타적이다."""

    DIFFED = "DIFFED"
    """`change_set`을 새로 만들었다."""

    SKIPPED_DONE = "SKIPPED_DONE"
    """이미 처리된 MST다. 최근 N일 재확인의 정상 경로이며 실패가 아니다."""

    NO_PREVIOUS = "NO_PREVIOUS"
    """제정본이라 비교 대상이 없다. 정상적인 개정 유형이다."""

    FAILED = "FAILED"
    """수집·적재·비교 중 실패했다. 다른 법령은 계속 처리한다."""


@dataclass(frozen=True, slots=True)
class DetectedLaw:
    """폴링이 알려 준 법령 버전 하나. 코퍼스 교집합만 여기까지 온다."""

    reg_date: str
    """8자리 공포일자. 법제처가 준 값 그대로이며 변환하지 않는다."""

    law_id: str
    law_name: str | None
    mst: str


@dataclass(frozen=True, slots=True)
class DateProbe:
    """날짜 하나의 폴링 결과.

    목적:
        "그날 무엇을 보았는가"를 실행 기록에 남긴다.

    구현 이유:
        `total_count`(법제처 전체 법령 수)와 `matched`(코퍼스 교집합)를 **둘 다**
        담는다. 교집합만 남기면 "0건인 날"이 두 가지 이유로 0이 되는데
        (법제처에 아무 개정이 없었다 / 개정은 있었지만 우리 대상이 아니다) 그
        구별이 사라진다. 앞의 경우가 반복되면 요청이 잘못 나가고 있다는 신호다.

    트레이드오프:
        폴링 응답 원문을 항상 보관하지는 않는다(`daily` 모듈의 트레이드오프 절).
        그래서 이 숫자가 사후 검증의 유일한 근거인 날이 있다.

    엣지 케이스:
        - 요청 실패: `total_count`가 None이다. 0과 구별된다.
    """

    reg_date: str
    status: IngestRunStatus
    total_count: int | None
    matched: int
    detail: str

    @property
    def failed(self) -> bool:
        """이 날짜를 신뢰할 수 없는가. 0건 미확인도 실패다."""
        return self.status in {
            IngestRunStatus.FAILED,
            IngestRunStatus.FAILED_ZERO_UNCONFIRMED,
        }


@dataclass(frozen=True, slots=True)
class LawOutcome:
    """법령 버전 하나의 처리 결과.

    목적:
        실패한 법령과 사유를 실행 기록에 남긴다 — 한 법령의 실패가 실행 전체를
        실패시키지 않는다는 결정의 기록면이다.

    구현 이유:
        `change_ratio_exceeded`와 `mst_resolution_source`를 여기까지 끌어올린다.
        `change_set`에 이미 있는 값이지만, **아무도 그 값을 보지 않으면 없는 것과
        같다**(R-21 잔여 리스크). `ops alerts`가 조인 없이 읽을 수 있어야 한다.

    트레이드오프:
        `change_set`과 값이 중복된다. 중복된 값은 어긋날 수 있다. 어긋나면 어느
        쪽이 옳은가 — `change_set`이 옳다. 이 행은 그 시점의 관측 사본이며,
        원본을 대체하지 않는다.

    엣지 케이스:
        - `FAILED`가 아니면 `failure_detail`은 None이어야 한다. DB CHECK가 강제한다.
        - `NO_PREVIOUS`/`SKIPPED_DONE`은 `articles_changed`가 None이다. 0이 아니다 —
          0은 "비교했고 변경이 없었다"이다.
    """

    detected: DetectedLaw
    status: LawOutcomeStatus
    change_set_id: UUID | None = None
    from_mst: str | None = None
    mst_resolution_source: str | None = None
    change_ratio_exceeded: bool | None = None
    articles_changed: int | None = None
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class DailyRunResult:
    """일일 실행 하나의 결과 전체. DB 기록과 표준출력이 이 값 하나를 읽는다."""

    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime
    status: OpsRunStatus
    lookback_days: int
    target_dates: tuple[str, ...]
    canary: CanaryResult
    probes: tuple[DateProbe, ...]
    outcomes: tuple[LawOutcome, ...]
    requests: int
    retries: int
    detail: str

    def count(self, status: LawOutcomeStatus) -> int:
        """처분별 법령 버전 수."""
        return sum(1 for outcome in self.outcomes if outcome.status is status)

    @property
    def change_sets_created(self) -> int:
        """새로 만든 `change_set` 수."""
        return self.count(LawOutcomeStatus.DIFFED)

    @property
    def articles_changed(self) -> int:
        """변경된 조문 수 합계. 적재 행 수가 아니다."""
        return sum(outcome.articles_changed or 0 for outcome in self.outcomes)


def lookback_dates(now: dt.datetime, days: int) -> tuple[str, ...]:
    """폴링 대상 날짜를 오래된 순서로 만든다 — **오늘은 넣지 않는다**.

    목적:
        "어제부터 N일"을 한 곳에서 계산해, 창 계산이 호출부마다 흩어지지 않게 한다.

    구현 이유:
        **오늘을 넣지 않는 이유**는 `regDt`가 공포일자이고 법제처 등재가 그날 안에
        끝난다는 보장이 없기 때문이다(ADR-005). 당일 폴링은 정상적으로 0건을
        돌려줄 수 있고, 그 0은 "개정 없음"으로 하류에 흘러간다. 어제부터 시작하면
        그 창을 애초에 만들지 않는다.

        **오래된 순서로 돌려주는 이유**는 처리 순서가 곧 적재 순서이고, 같은 법령이
        창 안에서 두 번 개정됐다면 옛 버전을 먼저 처리해야 diff 짝이 자연스럽기
        때문이다. 역순으로 돌면 새 버전을 먼저 적재하고 그 다음 옛 버전을 적재하는데,
        `oldAndNew`가 짝을 알려 주므로 결과는 같지만 **로그를 시간순으로 읽을 수
        없다.**

        KST 달력을 쓴다 — `OPS_CALENDAR_OFFSET` 참조. UTC로 계산하면 07:00 KST
        실행이 전날을 "오늘"로 본다.

    트레이드오프:
        같은 날짜를 매일 다시 폴링하므로 호출이 중복된다. 그 대가로 등재 지연과
        미실행일을 회수한다. 중복 처리가 데이터를 부풀리지 않는 것은 멱등성이
        보장한다 — 이미 처리한 MST는 `SKIPPED_DONE`으로 끝난다.

    엣지 케이스:
        - `days`가 1 이하: 어제 하루만 돌려준다. 0이나 음수는 `ValueError` —
          빈 창은 "볼 것이 없었다"로 위장한다.
        - naive datetime: `ValueError`. 어느 순간인지 알 수 없는 시각으로 달력을
          계산하지 않는다 (원칙 6).
        - 월·연 경계: `timedelta` 산술이므로 자연히 넘어간다.
    """
    if days < 1:
        raise ValueError(
            f"재확인 일수는 1 이상이어야 한다 (받은 값 {days}). 빈 창은 0건으로 위장한다"
        )
    if now.tzinfo is None:
        raise ValueError("naive datetime 으로 폴링 창을 계산하지 않는다 (원칙 6)")

    today = (now.astimezone(dt.UTC) + OPS_CALENDAR_OFFSET).date()
    return tuple(
        (today - dt.timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days, 0, -1)
    )


def derive_status(
    *,
    canary_passed: bool,
    probes: tuple[DateProbe, ...],
    outcomes: tuple[LawOutcome, ...],
) -> OpsRunStatus:
    """실행 상태를 관측값에서 파생시킨다 — **호출부가 주장하지 않는다**.

    목적:
        "오늘 실행이 성공인가"의 정의를 한 곳에 못박는다.

    구현 이유:
        호출부가 상태를 직접 정하게 두면 예외 경로마다 다른 판정이 생기고, 운영
        실적 숫자가 그 판정에 따라 달라진다. `MstResolutionSource`를 대조로
        파생시킨 것과 같은 이유다 — **주장과 사실이 갈릴 수 있는 자리를 없앤다.**

        판정 순서가 곧 우선순위다. 카나리아 실패는 다른 무엇보다 먼저다(수집을
        하지 않았으므로 뒤의 판정에 쓸 관측값이 없다). 그 다음이 전량 실패,
        부분 실패, 0건 순이다.

    트레이드오프:
        `PARTIAL`이 "날짜 일부 실패"와 "법령 일부 실패"를 합친다. 나누려면 상태가
        둘 더 필요한데, **둘 다 할 일이 같다** — `ops history`에서 사유를 열어
        본다. 상태는 집계축이고 사유는 상세다.

    엣지 케이스:
        - 폴링 대상 날짜가 0건: 발생하지 않는다(`lookback_dates`가 막는다).
          그래도 오면 `probes`가 비어 `FAILED`로 떨어지지 않고 `SUCCEEDED_ZERO`가
          된다 — 처리한 것이 없으므로 그것이 사실이다.
        - 카나리아는 통과했는데 모든 날짜가 실패: `FAILED`. 카나리아가 보지 못하는
          틈이며 `confirm_zero` docstring이 설명하는 실패다.
    """
    if not canary_passed:
        return OpsRunStatus.SKIPPED_CANARY_FAILED

    failed_dates = sum(1 for probe in probes if probe.failed)
    if probes and failed_dates == len(probes):
        return OpsRunStatus.FAILED

    failed_laws = sum(1 for outcome in outcomes if outcome.status is LawOutcomeStatus.FAILED)
    if failed_dates or failed_laws:
        return OpsRunStatus.PARTIAL

    processed = sum(
        1
        for outcome in outcomes
        if outcome.status in {LawOutcomeStatus.DIFFED, LawOutcomeStatus.NO_PREVIOUS}
    )
    return OpsRunStatus.SUCCEEDED if processed else OpsRunStatus.SUCCEEDED_ZERO
