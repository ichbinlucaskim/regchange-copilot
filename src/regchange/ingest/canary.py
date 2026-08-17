"""카나리아와 0건 재요청. 이력 계열의 구별 불가능한 0건에 대한 유일한 방어.

목적:
    수집 시작 전에 **알려진 비-0 날짜**를 호출해 파이프라인이 살아 있는지 확인하고,
    0건인 날은 **독립적으로 한 번 더 요청**해 같은 0인지 확인한다.

구현 이유:
    이력 계열에서 정상 0건과 요청 실패는 **응답으로 구별할 수 없다**
    (`HISTORY_ZERO_IS_AMBIGUOUS`). 응답 형태 분류로 가릴 수 없으므로 방어가
    응답 밖에 있어야 한다.

    **두 장치가 서로 다른 틈을 막는다.**

    | 장치 | 무엇을 보는가 | 보지 못하는 것 |
    |---|---|---|
    | 카나리아 | 파이프라인 전체가 살아 있는가 | 개별 날짜 요청이 제대로 갔는가 |
    | 0건 재요청 | 그 날짜의 0이 재현되는가 | 지속적 실패(둘 다 같이 실패) |

    카나리아만 두면 "세션은 살아 있는데 오늘 요청 파라미터가 틀렸다"를 놓치고,
    재요청만 두면 "두 요청이 같은 이유로 같이 실패했다"를 놓친다.

    **0으로 기록하는 것보다 "수집 안 함"이 안전하다** (ADR-005). 수집이 실패했는데
    "그날 개정 없음"으로 보이면 담당자는 **확인할 것이 없다고 잘못 안다** — 시스템이
    "아무 일도 없었다"는 판단을 대신 내려 버린 것이다.

트레이드오프:
    호출이 하루 2회 늘어난다(카나리아 1회 + 0건인 날 재요청 1회). 12개월 관측에서
    0건인 날이 161일이므로 연간 약 526회 추가다. **그 비용으로 그날 수집 전체의
    신뢰도가 보장된다.**

    카나리아 판정을 "0이 아니고 파싱 가능"으로 느슨하게 잡았다. 정확한 값 대조를
    포기한 이유는 아래 `CANARY_EXPECTED_COUNT` 참조.

엣지 케이스:
    - 카나리아 날짜가 측정 구간과 겹치면 집계를 오염시킨다. `2025-04-01`은 운영
      수집 대상(오늘)과 절대 겹치지 않는 과거 날짜다.
    - 카나리아가 실패하면 **본 수집을 아예 시작하지 않는다.** 0으로 기록하지 않는다.
    - 재요청 결과가 다르면(첫 번째 0, 두 번째 비-0) **경고가 아니라 실패**다.
      API가 불안정하거나 우리 요청이 비결정적이라는 뜻이며 둘 다 심각하다.
    - 두 장치 모두 **이력 계열에만 의미가 있다.** 본문·검색 계열은 0건 판정이
      필요하지 않거나 `resultCode`가 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from regchange.ingest.client import (
    Collection,
    CollectionFailure,
    CollectionOutcome,
    LawApiClient,
)
from regchange.ingest.response import JO_HISTORY_BY_DATE

_log = structlog.get_logger(__name__)

CANARY_DATE = "20250401"
"""카나리아 날짜. `dayjochg_regdt20250401.xml`에서 `totalCnt=83`으로 확인된 날이다.

운영 수집 대상(오늘)과 겹치지 않는 과거 날짜여야 한다 — 겹치면 카나리아 호출이
그날 집계를 오염시킨다. 0.8단계도 같은 날짜를 썼고 측정 구간(2025-08-01~) 밖이었다."""

CANARY_EXPECTED_COUNT = 83
"""카나리아의 기대 `totalCnt`. **판정 기준이 아니라 참조값이다.**

판정은 "0이 아니고 파싱 가능"으로 한다. 정확한 값 대조를 하지 않는 이유:

법제처 DB는 갱신된다. 과거 날짜의 조문 개정 이력에 누락이 보완되거나 정정이
반영되면 `totalCnt`가 83에서 달라질 수 있다. **그때 정확한 값 대조는 정상 상태를
"카나리아 실패"로 판정하고, 그 결과는 수집을 아예 하지 않는 것이다** — 즉
법제처의 데이터 정정이 우리 수집을 멈춘다.

카나리아가 답해야 하는 질문은 "값이 83인가"가 아니라 **"요청이 제대로 가서 비-0
응답이 오는가"**다. 그 질문에는 `> 0`으로 충분하다.

**다만 값이 83에서 달라지면 경고 로그를 남긴다.** 실패로 처리하지는 않지만, 기준값이
낡았다는 사실은 알아야 한다 — 조용히 지나가면 몇 년 뒤 이 상수가 무슨 근거였는지
아무도 모른다. 0.8단계 14회 호출에서 전부 정확히 83이었으므로 이 경고는 실제로
드물게 발화할 것이다.

0.8단계와의 차이: 그 스크립트는 `CANARY_EXPECTED_MIN = 1`로 `>= 1`만 검사하고
**83에서 벗어나도 경고하지 않았다.** 운영에서는 경고를 추가해 더 엄격하게 한다."""

CANARY_MIN_COUNT = 1
"""카나리아 통과의 최소 `totalCnt`. 0.8단계의 `CANARY_EXPECTED_MIN`과 같은 값이다."""


class IngestRunStatus(StrEnum):
    """수집 실행 하나의 결과 상태 — "실패했다"와 "안 했다"를 구별한다.

    목적:
        실행 이력에서 "수집이 실패한 날"과 "수집을 시도하지 않은 날"을 다른 값으로
        남긴다.

    구현 이유:
        두 상태를 하나로 묶으면 "그날 데이터가 없는 이유"를 사후에 알 수 없다.
        특히 `SKIPPED_CANARY_FAILED`는 **우리가 의도적으로 수집하지 않은 것**이며,
        그날의 0건 부재가 "개정이 없었다"는 뜻이 아님을 기록해야 한다.

        `SUCCEEDED_ZERO`를 따로 둔 이유도 같다. 0건 성공은 정상이지만 **연속 N일
        0건 알람**(ADR-005 방어 4, 작업 3)이 세어야 하는 대상이므로 값으로
        구별해 둔다.

    트레이드오프:
        상태가 다섯이라 호출부 분기가 늘어난다. 그 대신 실행 이력이 "왜 데이터가
        없는가"에 답할 수 있다 — 감사 대응의 핵심 질문이 그것이다 (원칙 6).

    엣지 케이스:
        - `SKIPPED_CANARY_FAILED`는 실패가 아니라 **미수행**이다. 실패율 지표에
          넣으면 우리 성능이 아닌 것을 우리 성능으로 계상한다.
        - `FAILED_ZERO_UNCONFIRMED`는 0건 재요청이 다른 결과를 낸 경우다.
          `docs/incidents/` 기록 후보다.
    """

    SUCCEEDED = "SUCCEEDED"
    """수집 성공. 비-0 건수를 받았고 무결성을 통과했다."""

    SUCCEEDED_ZERO = "SUCCEEDED_ZERO"
    """0건으로 수집 성공. 재요청으로 확인됐다. 연속 0건 알람의 계수 대상이다."""

    FAILED = "FAILED"
    """수집 실패. 형태 실패·전송 실패·완주 실패 등."""

    FAILED_ZERO_UNCONFIRMED = "FAILED_ZERO_UNCONFIRMED"
    """0건 재요청이 다른 결과를 냈다. **사건 기록 후보다.**"""

    SKIPPED_CANARY_FAILED = "SKIPPED_CANARY_FAILED"
    """카나리아 실패로 **수집을 하지 않았다.** 실패가 아니라 미수행이다."""


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """카나리아 호출 결과.

    목적:
        본 수집을 진행할지 판정하고, 판정 근거를 실행 메타데이터에 남긴다.

    구현 이유:
        불리언이 아니라 관측값(`total_count`)을 함께 담는다. 실패했을 때 "왜"를
        알아야 조치가 되고, 성공했을 때도 기준값 이동을 추적해야 한다.

    트레이드오프:
        없음이라 쓰지 않는다 — 담는 정보가 적어 트레이드오프가 생길 여지가 없다.
        굳이 말하면 실패 상세를 문자열로 담아 구조화 질의가 안 되는 점이며,
        이 값은 사람이 읽는 알람에만 쓰이므로 문제가 되지 않는다.

    엣지 케이스:
        - `total_count`가 None이면 응답 자체가 실패한 것이다(형태·전송).
        - `drifted`는 실패가 아니다. 경고 대상이며 본 수집은 진행한다.
    """

    passed: bool
    total_count: int | None
    detail: str

    @property
    def drifted(self) -> bool:
        """기준값(83)에서 벗어났는가 (실패가 아니라 경고 대상이다)."""
        return self.total_count is not None and self.total_count != CANARY_EXPECTED_COUNT


async def probe_canary(client: LawApiClient) -> CanaryResult:
    """알려진 비-0 날짜를 호출해 파이프라인이 살아 있는지 확인한다.

    목적:
        본 수집을 시작해도 되는지 판정한다. 실패하면 **그날 수집을 하지 않는다.**

    구현 이유:
        **매 실행마다 호출한다.** 0.8단계는 30일 주기(365일에 14회)였는데, 그것은
        대량 수집 스크립트였기 때문이다 — 주기적 확인으로 "직전 카나리아 이후의
        0건들"을 묶어 무효화하는 방식이 성립했다. 운영에서는 하루 수집이 한 단위
        이므로 **그날의 신뢰도를 그날 보장해야 한다.** 호출 하나 늘어나는 비용으로
        그날 수집 전체의 신뢰도가 보장된다.

        완주 검사를 포함한 `collect`를 쓴다. 카나리아가 첫 페이지만 보고 통과하면
        페이지네이션이 깨진 상태를 통과시킨다 — 카나리아 날짜는 83건이므로
        `PAGE_SIZE=100` 단일 페이지지만, **그 사실에 의존하지 않는다.**

    트레이드오프:
        호출이 하루 1회 늘어난다. 모듈 docstring의 트레이드오프 절 참조.

        판정을 느슨하게(`> 0`) 잡아 "83이 아닌 비-0"을 통과시킨다. 그래서 법제처가
        그 날짜의 데이터를 크게 줄이는 이상 상황을 잡지 못한다. `CANARY_EXPECTED_COUNT`
        docstring의 근거대로, 그 위험보다 **정상 정정을 실패로 판정하는 위험**이 크다.

    엣지 케이스:
        - 응답이 형태 실패·전송 실패: `passed=False`. 본 수집을 하지 않는다.
        - `totalCnt=0`: `passed=False`. 알려진 비-0 날짜가 0으로 오는 것은 요청이
          제대로 가지 않았다는 뜻이다.
        - `totalCnt`가 83이 아닌 비-0: `passed=True` + **경고 로그.** 기준값이
          낡았다는 신호이며 실패로 처리하지 않는다.
        - 완주 실패: `passed=False`. 페이지네이션이 깨진 상태로 본 수집을 시작하면
          그날 데이터가 조용히 잘린다.
    """
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": CANARY_DATE})

    if isinstance(outcome, CollectionFailure):
        detail = (
            f"카나리아 실패({CANARY_DATE}): {outcome.reason.value} — {outcome.detail}. "
            "본 수집을 시작하지 않는다. 0으로 기록하는 것보다 '수집 안 함'이 안전하다"
        )
        _log.error(
            "canary.failed",
            date=CANARY_DATE,
            reason=outcome.reason.value,
            detail=outcome.detail,
        )
        return CanaryResult(passed=False, total_count=None, detail=detail)

    observed = outcome.total_count
    if observed is None or observed < CANARY_MIN_COUNT:
        detail = (
            f"카나리아 실패({CANARY_DATE}): totalCnt={observed}. 알려진 비-0 날짜가 "
            "0으로 왔다 — 요청이 제대로 가지 않았다는 뜻이다. 본 수집을 시작하지 않는다"
        )
        _log.error("canary.failed", date=CANARY_DATE, total_count=observed)
        return CanaryResult(passed=False, total_count=observed, detail=detail)

    result = CanaryResult(
        passed=True,
        total_count=observed,
        detail=f"카나리아 통과({CANARY_DATE}): totalCnt={observed}",
    )
    if result.drifted:
        _log.warning(
            "canary.baseline_drifted",
            date=CANARY_DATE,
            observed=observed,
            expected=CANARY_EXPECTED_COUNT,
            message=(
                f"카나리아 기준값이 {CANARY_EXPECTED_COUNT}에서 {observed}로 달라졌다. "
                "법제처 DB 갱신일 수 있으므로 실패로 처리하지 않는다. "
                "기준값 상수를 갱신할지 검토하라"
            ),
        )
    else:
        _log.info("canary.passed", date=CANARY_DATE, total_count=observed)
    return result


@dataclass(frozen=True, slots=True)
class ZeroConfirmation:
    """0건 재요청 결과.

    목적:
        "0이 재현되었는가"를 판정하고 근거를 남긴다.

    구현 이유:
        `bool`이 아니라 두 관측값을 담는다. **재요청이 다른 값을 냈다면 그 값이
        무엇이었는지가 사건 기록의 핵심**이며, 불리언만 남기면 그것을 잃는다.

        0.8단계 스크립트는 `confirm_zero()`가 `bool`을 반환하고 **두 가지 다른
        결과를 같은 `False`로 뭉갰다** — (a) 재요청이 형태 실패, (b) 재요청이
        정상인데 비-0. **(b)가 훨씬 심각하다** — 첫 요청이 조용히 적게 셌다는
        뜻이다. 그 구별을 살리는 것이 이 타입의 존재 이유다.

    트레이드오프:
        필드가 셋이라 호출부가 조합을 판단해야 한다. `confirmed` 프로퍼티로
        단일 판정을 제공해 그 부담을 줄였다.

    엣지 케이스:
        - `second_count`가 None이면 재요청 자체가 실패한 것이다.
        - `second_count`가 0이 아니면 **첫 요청이 적게 셌다는 뜻이며 실패다.**
    """

    first_count: int
    second_count: int | None
    detail: str

    @property
    def confirmed(self) -> bool:
        """재요청도 정상이고 같은 0인가."""
        return self.second_count == 0

    @property
    def is_incident_candidate(self) -> bool:
        """재요청이 성공했는데 값이 달랐는가 (사건 기록 후보다)."""
        return self.second_count is not None and self.second_count != self.first_count


async def confirm_zero(client: LawApiClient, params: dict[str, str]) -> ZeroConfirmation:
    """0건인 날을 독립적으로 한 번 더 요청해 같은 0인지 확인한다.

    목적:
        카나리아가 보지 못하는 틈 — **개별 날짜 요청이 제대로 갔는가** — 를 메운다.

    구현 이유:
        이력 계열의 0건은 정상과 실패가 응답으로 구별되지 않으므로, **독립적인 두
        번째 관측**으로 검증한다. 두 요청이 같은 0을 주면 일시적 실패일 가능성이
        낮아진다. 지속적 실패는 카나리아가 담당한다.

        **0.8단계와의 판정 차이 (엄격한 쪽으로 맞췄다).** 그 스크립트는
        `confirm_zero()`가 `False`를 반환해도 `zero_confirmed[stamp] = False`로
        기록하고 **수집을 계속했다.** 리포트 마지막에 "재요청 미확인 N일"로만
        보고했다. 탐색 도구로는 합리적이다 — 미확인 날짜를 알고 집계에서 판단하면
        된다.

        운영에서는 **실패로 처리한다.** 0을 기록하면 그것이 "개정 없음"으로 하류에
        흘러가고, 담당자는 확인할 것이 없다고 잘못 안다. 미확인 상태를 데이터에
        남기면 그 판단이 이미 내려진 뒤다.

        (참고: 0.8단계 실측에서 161일 전부 `zero_confirmed=True`였다. 이 엄격화가
        실제로 수집을 막은 사례는 아직 없다.)

    트레이드오프:
        0건인 날마다 호출이 1회 늘어난다. 12개월 관측에서 161일이므로 연간 약
        161회다. 무시할 비용이다.

        같은 파라미터로 같은 API를 부르므로 **완전히 독립적인 관측이 아니다** —
        재계산 판별 원칙 2("재계산은 원본과 다른 경로여야 한다")를 완전히 만족하지
        못한다. 다른 경로가 존재하지 않기 때문이다: `lsHstInf`는 `regDt` 의미가
        달라 대조에 쓸 수 없다 (ADR-005 근거 3). **그 한계를 알고 쓴다** — 이것이
        잡는 것은 일시적 실패이며 계통적 실패는 카나리아가 본다.

    엣지 케이스:
        - 재요청이 형태·전송 실패: `second_count=None`, 확인 실패.
        - 재요청이 비-0: `is_incident_candidate=True`. **첫 요청이 조용히 적게
          셌다는 뜻이며 사건 기록 후보다.**
        - 재요청도 0: 확인 성공. 수집을 0건으로 커밋한다.
    """
    outcome: CollectionOutcome = await client.collect(JO_HISTORY_BY_DATE, params)

    if isinstance(outcome, CollectionFailure):
        detail = (
            f"0건 재요청 실패: {outcome.reason.value} — {outcome.detail}. "
            "0을 확인하지 못했으므로 0으로 기록하지 않는다"
        )
        _log.error("zero_recheck.failed", reason=outcome.reason.value, detail=outcome.detail)
        return ZeroConfirmation(first_count=0, second_count=None, detail=detail)

    second = outcome.total_count or 0
    if second != 0:
        detail = (
            f"0건 재요청이 다른 결과를 냈다: 첫 요청 0건, 재요청 {second}건. "
            "**첫 요청이 조용히 적게 셌다는 뜻이다.** API가 불안정하거나 우리 요청이 "
            "비결정적이며 둘 다 심각하다 — docs/incidents/ 기록 후보다"
        )
        _log.error(
            "zero_recheck.mismatch",
            first_count=0,
            second_count=second,
            message="사건 기록 후보. 첫 요청이 적게 셌다",
        )
        return ZeroConfirmation(first_count=0, second_count=second, detail=detail)

    _log.info("zero_recheck.confirmed", count=0)
    return ZeroConfirmation(
        first_count=0, second_count=0, detail="0건 재요청 확인. 두 요청이 같은 0을 반환했다"
    )


@dataclass(frozen=True, slots=True)
class DailyIngestResult:
    """하루 수집 실행 하나의 결과. 상태와 근거를 함께 담는다.

    목적:
        "그날 무엇을 했고 왜 그 상태인가"를 한 값으로 남긴다.

    구현 이유:
        `collection`이 None인 상태가 여러 이유로 발생한다(카나리아 실패, 수집 실패,
        0건 미확인). **`status`가 그 이유를 구별하며, None 자체는 아무것도 말하지
        않는다** — 0건과 실패를 섞지 않는다는 규칙의 실행 단위 적용이다.

    트레이드오프:
        `collection`이 Optional이라 호출부가 검사해야 한다. `status`로 분기하면
        타입 좁히기가 되지 않는 점이 불편하지만, 상태가 다섯이라 타입을 다섯으로
        나누면 그쪽이 더 무겁다.

    엣지 케이스:
        - `SKIPPED_CANARY_FAILED`에서 `collection`은 None이고 **재시도 통계도
          비어 있지 않다** — 카나리아 호출 자체의 통계가 남는다.
    """

    status: IngestRunStatus
    detail: str
    collection: Collection | None = None
    canary: CanaryResult | None = None
    zero_confirmation: ZeroConfirmation | None = None

    @property
    def collected(self) -> bool:
        """수집이 실제로 커밋 가능한 상태인가."""
        return self.status in {IngestRunStatus.SUCCEEDED, IngestRunStatus.SUCCEEDED_ZERO}


async def run_daily_ingest(client: LawApiClient, promulgation_date: str) -> DailyIngestResult:
    """카나리아 → 본 수집 → 0건 재요청 순으로 하루치를 수집한다.

    목적:
        ADR-005 트랙 1의 공포 감지 경로. `regDt`(공포일자)로 그날 조문 개정 이력을
        받는다.

    구현 이유:
        **카나리아를 본 수집보다 먼저 호출한다.** 순서가 뒤바뀌면 카나리아가
        "이미 기록한 0"을 사후에 무효화하는 일이 되고, 그 무효화를 하류가 따라가야
        한다. 먼저 확인하면 잘못된 0이 애초에 만들어지지 않는다.

        `regDt`는 **공포일자**다 (ADR-005). 시행일이 아니다 — 시행일 도래 감시는
        외부 API를 쓰지 않고 내부 DB를 읽는 트랙 2이며 작업 3 이후다.

    트레이드오프:
        카나리아 실패 시 그날 데이터가 아예 없다. 부분 수집보다 데이터 부재가
        낫다는 판단이며(ADR-005), 대신 **알람이 반드시 사람에게 닿아야 한다** —
        조용히 건너뛰면 "개정 없음"과 구별되지 않는다. 알람 전달은 이 함수의
        책임이 아니고 `dispatch`가 담당한다.

    엣지 케이스:
        - 카나리아 실패: `SKIPPED_CANARY_FAILED`. 본 수집을 호출하지 않는다.
        - 수집 실패: `FAILED`. 부분 결과를 담지 않는다.
        - 0건이고 재요청 확인: `SUCCEEDED_ZERO`.
        - 0건인데 재요청 미확인/불일치: `FAILED_ZERO_UNCONFIRMED`. 0으로 기록하지
          않는다.
        - 비-0 수집 성공: `SUCCEEDED`. 재요청하지 않는다 — 비-0은 요청이 제대로
          갔다는 증거 자체다.
    """
    canary = await probe_canary(client)
    if not canary.passed:
        return DailyIngestResult(
            status=IngestRunStatus.SKIPPED_CANARY_FAILED,
            detail=canary.detail,
            canary=canary,
        )

    params = {"regDt": promulgation_date}
    outcome = await client.collect(JO_HISTORY_BY_DATE, params)
    if isinstance(outcome, CollectionFailure):
        return DailyIngestResult(
            status=IngestRunStatus.FAILED,
            detail=f"{promulgation_date} 수집 실패: {outcome.reason.value} — {outcome.detail}",
            canary=canary,
        )

    if outcome.total_count:
        return DailyIngestResult(
            status=IngestRunStatus.SUCCEEDED,
            detail=(
                f"{promulgation_date} 수집 성공. 법령 {outcome.report.item_count}건 / "
                f"조문 {outcome.report.article_count}건"
            ),
            collection=outcome,
            canary=canary,
        )

    confirmation = await confirm_zero(client, params)
    if not confirmation.confirmed:
        return DailyIngestResult(
            status=IngestRunStatus.FAILED_ZERO_UNCONFIRMED,
            detail=f"{promulgation_date}: {confirmation.detail}",
            canary=canary,
            zero_confirmation=confirmation,
        )
    return DailyIngestResult(
        status=IngestRunStatus.SUCCEEDED_ZERO,
        detail=f"{promulgation_date} 0건 수집 성공 (재요청 확인)",
        collection=outcome,
        canary=canary,
        zero_confirmation=confirmation,
    )
