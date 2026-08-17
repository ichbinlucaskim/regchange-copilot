"""법제처 API 클라이언트. 스로틀·재시도·페이지네이션 완주를 담당한다.

목적:
    요청을 보내고, 응답을 분류하고, 목록 응답은 **`totalCnt`를 다 받을 때까지**
    페이지를 넘긴 뒤 무결성을 단언해 반환한다. 불일치는 실패다.

구현 이유:
    HTTP 클라이언트(httpx)는 직접 쓰고 파일 저장만 어댑터를 통한다 (ADR-010).
    HTTP는 배포 형태에 따라 갈리지 않는다 — 망분리 환경에서도 같은 프로토콜로
    같은 공공 API를 호출한다. **어댑터로 감쌀 근거가 없는 것을 감싸면 ADR-010이
    경고한 과잉설계가 된다.**

    시간·난수 의존을 주입받는다(`sleep`, `jitter`). 재시도와 스로틀을 실제로
    기다리지 않고 검증하기 위해서이며, **테스트가 느려지면 그 테스트는 결국
    지워진다.**

    **요청 전에 필수 파라미터를 검증한다.** 이력 계열에서 파라미터 오류는 정상
    0건과 구별 불가능하므로(`HISTORY_ZERO_IS_AMBIGUOUS`), 응답을 보고 알아낼
    방법이 없다. `error_dayjochg_id_only_zero.xml`이 바로 그 사고이며 — `ID`만
    주고 `regDt`를 빼먹은 요청이 `totalCnt=0`으로 돌아왔다 — **우리 쪽 실수는
    보내기 전에 막는 것이 유일한 방어다.** 카나리아는 파이프라인 전체를 보지만
    개별 요청의 파라미터를 보지 못한다.

트레이드오프:
    `display`를 100으로 고정했다. 실측은 1,000도 반환하므로 페이지 수가 10배
    늘고 그만큼 호출이 늘어난다(1.2초 간격이므로 하루 최대 관측 1,464건이면
    15회 = 약 18초). **문서화되지 않은 동작에 의존하지 않기 위해 그 비용을
    지불한다** — 0.8단계 스크립트는 1,000을 썼고 그것은 탐색이라 허용됐다.
    운영 코드가 미문서화 동작에 의존하면 법제처가 그것을 바꾸는 날 조용히 잘린다.

    페이지를 전부 메모리에 모은 뒤 검사한다. 스트리밍하면 **완주 여부를 마지막에
    알게 되고, 그때는 이미 부분 데이터를 하류로 흘려보낸 뒤다.** 부분 상태를
    정상으로 커밋하지 않는다는 규칙(ADR-005)이 메모리 비용보다 우선한다.
    최대 관측 응답이 2.1MB이므로 상한이 있다.

엣지 케이스:
    - **응답 형태 실패는 재시도하지 않는다.** 파라미터가 틀린 것이므로 재시도해도
      같다. 재시도 대상은 `httpx.TransportError`뿐이다.
    - 페이지 중간에 형태 실패가 나면 **그 자리에서 실패**로 끝낸다. 받은 페이지만
      병합해 돌려주면 잘린 결과가 정상으로 보인다.
    - 진전 없는 응답을 **두 가지로** 잡는다. (a) 항목 0건, (b) **마지막 페이지가
      아닌데 페이지가 덜 찬 경우.** (b)를 넣은 이유는 구현 중 실측으로 발견한
      결함이다 — `totalCnt=84`에 1건짜리 페이지를 반복해서 주는 응답을 넣으면,
      매 페이지가 1건씩 "진전"하므로 (a)에 걸리지 않고 **같은 항목이 84번 누적된
      뒤 완주로 통과했다.** 정상 페이지네이션은 마지막 페이지 전까지 항상
      `display`만큼 채워 주므로, 덜 찬 페이지는 "API가 더 주지 않는다"는 신호다.
    - `MAX_PAGES` 상한을 함께 둔다. 위 두 검사를 통과하면서도 끝나지 않는 미지의
      형태에 대한 마지막 방어선이다.
    - 본문 계열은 페이지네이션이 없다. `totalCnt`가 없어 완주 검사 대상이 아니며
      단일 요청으로 끝난다.
    - 마스킹은 `classify`가 파싱보다 먼저 수행한다. 이 모듈이 다루는 본문 문자열은
      전부 마스킹된 뒤이며, **원본 바이트는 응답 객체 밖으로 나가지 않는다.**
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from xml.etree.ElementTree import Element

import httpx
import structlog

from regchange.ingest.integrity import IntegrityReport, check_integrity
from regchange.ingest.masking import Masker
from regchange.ingest.response import (
    Classified,
    ClassifiedFailure,
    ResponseFamily,
    ResponseKind,
    TargetSpec,
    classify,
)
from regchange.ingest.vocabulary import find_unknown_values

_log = structlog.get_logger(__name__)

MIN_CALL_INTERVAL_SECONDS = 1.2
"""호출 간 최소 간격. `docs/security-notes.md` §5의 운영 규칙이며 0.8단계에서
1.2초 간격 540여 회에 실패 0건으로 확인됐다. **rate limit의 실제 한계는 미확인**
이므로 관측된 안전 구간을 유지한다."""

REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
"""단일 타임아웃. `read=60초`는 최대 실측 6.1초(자본시장법 2.1MB)의 약 10배다.

**용도별로 나누지 않았다.** 목록/본문 경계가 `display`에 따라 흐려지고(목록 응답이
1,464건이면 작지 않다), 목록이 15초를 넘기는 상황은 이미 비정상이므로 어느 값에서
끊든 결과가 같다 — 둘 다 실패로 끝난다. 상수를 둘로 나누면 호출부가 어느 쪽인지
판단해야 하고 **그 판단이 틀릴 자리가 생긴다.**

`connect=10초`는 실측과 무관하게 망 문제를 조기에 드러내기 위한 값이다. 연결이
10초 걸리는 환경에서는 본 수집을 시도할 의미가 없다."""

MAX_ATTEMPTS = 3
"""시도 총 횟수(최초 1회 + 재시도 2회). 일시적 네트워크 흔들림 한 번은 흡수하고
그 이상이면 수집 실패로 올린다. 매일 1회 수집이므로 다음 실행까지 24시간이 있다."""

BACKOFF_BASE_SECONDS = 1.2
"""백오프 시작값. **`MIN_CALL_INTERVAL_SECONDS`와 같은 값이며 우연이 아니다.**

재시도가 공공 API에 대한 최소 간격 정책보다 촘촘해지면 안 된다. 그리고 백오프
대기는 최소 간격을 **대체하는 것이 아니라 그 위에 얹힌다** — 백오프 뒤에도 스로틀이
다시 적용된다."""

BACKOFF_JITTER_RATIO = 0.2
"""백오프에 곱하는 지터 폭(±20%).

지금은 단일 프로세스지만 ADR-010의 SQS 워커 구조에서는 여러 태스크가 동시에 돌 수
있다. 그때 재시도가 동기화되면 같은 순간에 몰려 공공 API에 스파이크를 만든다."""

PAGE_SIZE = 100
"""`display` 값. 활용가이드상 최대이며 **실측 1,000에 의존하지 않는다** (§7.1).
미문서화 동작에 의존하면 법제처가 그것을 바꾸는 날 조용히 잘린다."""

MAX_PAGES = 200
"""페이지 상한. 하루 최대 관측이 법령 1,464건(2025-10-01)이므로 `PAGE_SIZE=100`에서
15페이지다. 200은 그 13배이며 20,000건에 해당한다 — 0.8단계 스크립트의 상한
(1,000 곱하기 20 = 20,000)과 같은 천장을 다른 페이지 크기로 유지한다."""


class CollectionFailureReason(StrEnum):
    """수집이 실패한 사유. 응답 형태 실패와 수집 절차 실패를 구별한다.

    목적:
        "무엇을 고쳐야 하는가"를 값으로 남긴다.

    구현 이유:
        `ResponseKind`와 합치지 않았다. 형태 실패는 **응답 하나**의 성질이고
        수집 실패는 **여러 요청의 절차**에 대한 것이다. 합치면 "3페이지째에서
        HTML이 왔다"와 "완주하지 못했다"가 같은 축에 놓여, 어느 것이 재시도
        가능한지 구별되지 않는다.

    트레이드오프:
        호출부가 두 축을 봐야 한다. 그 대신 실패의 층위가 드러난다.

    엣지 케이스:
        - `BAD_REQUEST`는 요청을 **보내기 전에** 판정된다. 네트워크를 쓰지 않으므로
          호출 간격도 소비하지 않는다.
    """

    BAD_REQUEST = "BAD_REQUEST"
    """필수 파라미터 누락. 보내기 전에 막았다."""

    RESPONSE_SHAPE = "RESPONSE_SHAPE"
    """응답 형태 실패. 재시도하지 않는다."""

    TRANSPORT = "TRANSPORT"
    """네트워크 오류가 재시도 후에도 계속됐다."""

    INCOMPLETE = "INCOMPLETE"
    """완주 실패 또는 식별키 충돌. 무결성 보고서에 사유가 있다."""

    NO_PROGRESS = "NO_PROGRESS"
    """페이지를 넘겼는데 새 항목이 오지 않았다."""

    PAGE_LIMIT = "PAGE_LIMIT"
    """페이지 상한에 도달했다. `totalCnt`가 비정상적으로 크다."""


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """재시도 한 번의 기록. 조용한 열화를 드러내기 위한 것이다."""

    attempt: int
    waited_seconds: float
    error_type: str


@dataclass(slots=True)
class RequestStats:
    """한 수집 단위의 요청 통계. 실행 메타데이터에 집계한다.

    목적:
        호출 수와 재시도 횟수를 남겨, **조용히 세 번 재시도하고 성공한 것과 한 번에
        성공한 것을 구별한다.**

    구현 이유:
        전자는 네트워크나 API가 불안정하다는 신호다. 로그에만 남기면 집계가 안 되고,
        집계하지 않으면 **그 열화를 영영 모른다.** 그래서 실행 단위로 합산한다.

    트레이드오프:
        가변 객체다. 불변으로 만들면 페이지마다 새 객체를 만들어 합쳐야 하고
        호출부가 그 합산을 잊을 수 있다. 가변성을 감수하고 누락을 막았다.

    엣지 케이스:
        - `retries`가 0이면 재시도가 없었다는 뜻이며 정상이다. 그 사실도 기록한다.
    """

    requests: int = 0
    retries: int = 0
    records: list[RetryRecord] = field(default_factory=list)

    def note_retry(self, record: RetryRecord) -> None:
        """재시도 한 건을 기록한다."""
        self.retries += 1
        self.records.append(record)


@dataclass(frozen=True, slots=True)
class Collection:
    """완주와 무결성을 통과한 수집 결과.

    목적:
        하류(적재·파싱)가 쓸 항목과, 그 결과가 신뢰할 만하다는 증거를 함께 담는다.

    구현 이유:
        `report`를 필수 필드로 뒀다. 결과만 반환하면 **무결성 검사가 돌았는지
        하류가 알 수 없고**, 검사를 건너뛴 경로가 생겨도 드러나지 않는다.
        이 저장소의 사건 3건은 전부 "검증 로직이 잡은 것이 하나도 없다"였다.

    트레이드오프:
        `Element` 를 들고 있어 직렬화되지 않는다. 도메인 변환을 뒤로 미룬 대신
        원문 트리를 보존했다 (`ingest/__init__.py`).

    엣지 케이스:
        - `unknown_enum_values`가 비어 있지 않아도 **실패가 아니다.** 미지의 값은
          기록 대상이며 수집은 성공이다.
        - `bodies`는 페이지별 마스킹된 본문이다. 저장은 이 값으로만 한다.
    """

    spec: TargetSpec
    items: tuple[Element, ...]
    articles: tuple[Element, ...]
    bodies: tuple[str, ...]
    report: IntegrityReport
    stats: RequestStats
    unknown_enum_values: Mapping[str, tuple[str, ...]]

    @property
    def total_count(self) -> int | None:
        """`totalCnt`. 본문 계열은 None이다."""
        return self.report.total_count


@dataclass(frozen=True, slots=True)
class CollectionFailure:
    """수집이 실패했다 — 부분 결과를 담지 않는다.

    목적:
        실패를 성공과 다른 타입으로 표현해, 호출부가 부분 데이터를 쓰지 못하게 한다.

    구현 이유:
        받은 페이지를 함께 담고 싶은 유혹이 있지만 담지 않는다. **부분 상태를
        정상으로 커밋하면 diff가 "대량 삭제"로 오판한다** (ADR-005). 부분 데이터에
        접근할 수 없으면 그 사고가 성립하지 않는다.

        진단용 숫자(`received`, `expected`)는 담는다 — 그것은 데이터가 아니라
        사실이다.

    트레이드오프:
        재수집이 처음부터 다시 시작한다. 이어받기를 포기한 대신 부분 커밋 경로를
        없앴다. 하루 최대 15페이지이므로 재시작 비용이 작다.

    엣지 케이스:
        - `report`가 None일 수 있다. 무결성 검사에 도달하기 전에 실패한 경우다
          (형태 실패, 전송 실패). 어디까지 갔는지가 그 값으로 드러난다.
    """

    spec: TargetSpec
    reason: CollectionFailureReason
    detail: str
    stats: RequestStats
    expected: int | None = None
    received: int = 0
    response_kind: ResponseKind | None = None
    report: IntegrityReport | None = None


type CollectionOutcome = Collection | CollectionFailure
"""수집 결과. `isinstance(outcome, Collection)`으로 좁힌다."""


def _default_jitter() -> float:
    """백오프에 곱할 계수(1 ± 0.2)를 만든다.

    `random.SystemRandom`을 쓰는 이유는 암호학적 필요가 아니라, 모듈 수준
    `random.uniform`이 ruff S311에 걸리기 때문이다. **`noqa`를 붙이지 않기 위해
    이 형태를 택했다** — `src/`에 예외를 만들지 않는다는 ADR-012의 규칙과 같다.
    """
    return 1.0 + random.SystemRandom().uniform(-BACKOFF_JITTER_RATIO, BACKOFF_JITTER_RATIO)


class LawApiClient:
    """법제처 API 호출자. 스로틀·재시도·페이지네이션을 담당한다.

    목적:
        외부 API와 도메인 코드 사이의 유일한 통로. 요청 정책을 한 곳에 모은다.

    구현 이유:
        `httpx.AsyncClient`를 주입받는다. 생성을 안에서 하면 수명 관리가 이 클래스에
        갇히고, 테스트가 `MockTransport`를 넣을 수 없다.

        **OC 문자열을 받아 `Masker`를 안에서 만든다. `Masker`를 주입받지 않는다.**
        주입받으면 "보내는 자격증명"과 "마스킹하는 자격증명"이 갈릴 수 있고, 그때
        유출은 **조용하다** — 응답에 echo된 OC가 마스킹 대상과 달라 그대로 저장되며,
        마스킹은 성공한 것처럼 보인다. 안에서 만들면 그 불일치가 구조적으로
        불가능하다.

        테스트도 같은 경로를 쓴다 — 임의의 OC 문자열을 주면 그 값이 요청에 실리고
        같은 값이 마스킹된다. 마스킹 전용 우회 경로를 만들지 않았다.

    트레이드오프:
        스로틀 상태(마지막 호출 시각)를 인스턴스에 둔다. 인스턴스를 여럿 만들면
        간격이 지켜지지 않는다. 전역 상태를 피한 대신 **"클라이언트는 하나만
        만든다"는 규약**이 생겼다 — 전역 변수가 테스트를 오염시키는 것보다 낫다.

    엣지 케이스:
        - 첫 호출은 대기하지 않는다. 프로세스 시작 직후의 호출을 1.2초 늦출 이유가
          없다.
        - `sleep`/`jitter` 주입은 테스트 전용이 아니다. 운영에서도 기본값이
          그대로 쓰이므로 **테스트 전용 우회 경로가 아니다** (CLAUDE.md §5.2).
    """

    __slots__ = ("_base_url", "_http", "_jitter", "_last_call_at", "_masker", "_oc", "_sleep")

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        oc: str,
        *,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """기반 URL, HTTP 클라이언트, OC를 받는다. 마스커는 OC로부터 만든다.

        엣지 케이스:
            - `base_url` 끝의 `/`는 제거한다. 설정에 따라 있고 없으므로 조립 시
              이중 슬래시가 생기는 것을 막는다.
            - OC가 비어 있으면 `Masker`가 `MaskingError`를 던진다. 빈 자격증명으로
              요청을 보내면 응답은 실패하고 마스킹은 본문을 파괴하므로, **생성
              시점에 막는 것이 맞다.**
        """
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._oc = oc
        self._masker = Masker(oc)
        self._sleep = sleep if sleep is not None else time.sleep
        self._jitter = jitter if jitter is not None else _default_jitter
        self._last_call_at: float | None = None

    def _throttle(self) -> None:
        """최소 호출 간격을 지킨다. 첫 호출은 대기하지 않는다."""
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            if elapsed < MIN_CALL_INTERVAL_SECONDS:
                self._sleep(MIN_CALL_INTERVAL_SECONDS - elapsed)
        self._last_call_at = time.monotonic()

    def missing_params(self, spec: TargetSpec, params: Mapping[str, str]) -> frozenset[str]:
        """필수 파라미터 중 빠진 것을 반환한다. 보내기 전에 쓴다.

        목적:
            우리 쪽 파라미터 실수를 요청 전에 잡는다.

        구현 이유:
            이력 계열에서 파라미터 오류는 정상 0건과 **바이트 단위로 동일한 응답**을
            낸다. 응답을 보고는 알 수 없으므로 보내기 전에 막는 것이 유일한 방어다.
            `error_dayjochg_id_only_zero.xml`이 실제로 이 실수의 결과다.

        트레이드오프:
            "값이 존재하는가"만 본다. 형식(날짜 8자리, `JO` 6자리)은 검사하지
            않는다. 형식 검사를 여기 넣으면 spec에 검증 규칙이 쌓여 파라미터
            스키마가 되고, 그것은 이 클래스의 책임이 아니다. 형식은 호출부의
            타입으로 다룬다.

        엣지 케이스:
            - 값이 빈 문자열이면 빠진 것으로 본다. `regDt=`는 `regDt` 없음과
              같은 결과를 낸다.
            - `OC`는 필수 목록에 없다. 클라이언트가 항상 붙이므로 호출부의
              책임이 아니다.
        """
        return frozenset(
            name for name in spec.required_params if not (params.get(name) or "").strip()
        )

    async def fetch(
        self, spec: TargetSpec, params: Mapping[str, str], *, page: int | None = None
    ) -> Classified | CollectionFailure:
        """단일 요청을 보내고 응답을 분류한다. 네트워크 오류만 재시도한다.

        목적:
            스로틀·타임아웃·재시도를 적용한 한 번의 호출.

        구현 이유:
            **재시도 대상을 `httpx.TransportError`로 좁혔다.** 응답 형태 실패는
            파라미터가 틀린 것이므로 재시도해도 같은 응답이 온다 — 재시도하면
            공공 API에 무의미한 부하만 준다.

            재시도할 때마다 시도 순번·대기 시간·예외 유형을 로그에 남기고, 최종
            성공 시 "n회 재시도 후 성공"을 남긴다. **조용히 세 번 재시도하고
            성공하는 것과 한 번에 성공하는 것은 운영에서 다른 정보다.**

            분류에 바이트를 넘긴다. 인코딩 선언을 신뢰하지 않고 UTF-8로 고정
            해석하려면 디코딩을 분류기가 해야 한다 (edge-case #15).

        트레이드오프:
            `stats`를 반환값에 담지 않고 인자로 받는다. 반환 타입이 단순해지는
            대신 호출부가 객체를 준비해야 한다. 페이지네이션이 여러 호출을 합산해야
            하므로 이 방향이 맞다.

        엣지 케이스:
            - 필수 파라미터 누락: 네트워크를 쓰지 않고 `BAD_REQUEST`로 끝낸다.
            - 마지막 시도에서도 전송 오류: `TRANSPORT` 실패. 대기 없이 끝낸다
              (마지막 시도 뒤의 백오프는 아무것도 기다릴 것이 없다).
            - HTTP 상태코드는 검사하지 않는다. 실패도 200으로 오므로 아무것도
              구별하지 못한다 (edge-case #10).
        """
        stats = RequestStats()
        return await self._fetch_with_stats(spec, params, stats, page=page)

    async def _fetch_with_stats(
        self,
        spec: TargetSpec,
        params: Mapping[str, str],
        stats: RequestStats,
        *,
        page: int | None = None,
    ) -> Classified | CollectionFailure:
        """`fetch`의 본체. 통계를 누적할 수 있도록 `stats`를 받는다."""
        missing = self.missing_params(spec, params)
        if missing:
            detail = (
                f"{spec.key}: 필수 파라미터 누락 {sorted(missing)}. 요청을 보내지 않았다 — "
                "이력 계열에서 파라미터 오류는 정상 0건과 구별할 수 없으므로 "
                "보내기 전에 막는 것이 유일한 방어다"
            )
            _log.error("law_api.bad_request", spec=spec.key, missing=sorted(missing))
            return CollectionFailure(
                spec=spec,
                reason=CollectionFailureReason.BAD_REQUEST,
                detail=detail,
                stats=stats,
            )

        query: dict[str, str] = {
            "OC": self._oc,
            "target": spec.target,
            "type": "XML",
            **params,
        }
        url = f"{self._base_url}/{spec.endpoint}"
        if page is not None:
            query["page"] = str(page)

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            stats.requests += 1
            try:
                response = await self._http.get(url, params=query, timeout=REQUEST_TIMEOUT)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                waited = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) * self._jitter()
                stats.note_retry(
                    RetryRecord(
                        attempt=attempt, waited_seconds=waited, error_type=type(exc).__name__
                    )
                )
                _log.warning(
                    "law_api.retry",
                    spec=spec.key,
                    attempt=attempt,
                    max_attempts=MAX_ATTEMPTS,
                    waited_seconds=round(waited, 3),
                    error_type=type(exc).__name__,
                )
                self._sleep(waited)
                continue

            if attempt > 1:
                _log.info(
                    "law_api.retry_succeeded",
                    spec=spec.key,
                    retries=attempt - 1,
                    message=f"{attempt - 1}회 재시도 후 성공",
                )
            return classify(response.content, spec, masker=self._masker)

        detail = (
            f"{spec.key}: 전송 오류가 {MAX_ATTEMPTS}회 시도에도 계속됐다 "
            f"({type(last_error).__name__}: {last_error})"
        )
        _log.error(
            "law_api.transport_failed",
            spec=spec.key,
            attempts=MAX_ATTEMPTS,
            error_type=type(last_error).__name__,
        )
        return CollectionFailure(
            spec=spec,
            reason=CollectionFailureReason.TRANSPORT,
            detail=detail,
            stats=stats,
        )

    async def collect(self, spec: TargetSpec, params: Mapping[str, str]) -> CollectionOutcome:
        """`totalCnt`를 다 받을 때까지 페이지를 넘기고 무결성을 단언한다.

        목적:
            목록 응답을 **완주**해서 받고, 완주하지 못하면 실패로 끝낸다.

        구현 이유:
            **불일치는 경고가 아니라 실패다.** 응답에 "잘렸다"는 표시가 없으므로
            (edge-case #11) `totalCnt` 대조가 유일한 검출 수단이며, 경고로 두면
            잘린 데이터가 하류로 흘러간다. 이 저장소는 그렇게 어휘를 4종으로
            결론낸 적이 있다(사건 2).

            **본문 계열은 페이지네이션을 하지 않는다.** `totalCnt`가 없어 완주
            개념이 성립하지 않으며, 단일 요청으로 끝난다. 이 분기를 `family`에서
            파생시키므로 새 target마다 판단할 일이 없다.

            무한 루프를 두 가지로 막는다 — 진전 없는 응답 감지와 페이지 상한.
            둘 다 필요한 이유는, **진전이 있으면서도 끝나지 않는 응답**(같은 페이지가
            계속 오는 경우)을 상한만이 막기 때문이다.

        트레이드오프:
            페이지를 전부 모은 뒤 검사하므로 메모리를 쓴다. 모듈 docstring의
            트레이드오프 절 참조.

            첫 페이지의 `totalCnt`를 기준으로 삼는다. 수집 중에 `totalCnt`가 바뀌면
            (법제처가 그 사이에 데이터를 추가) 불일치로 실패한다. **그 실패가 맞는
            처리다** — 페이지마다 다른 모집단을 섞어 병합하면 무엇을 받았는지 알 수
            없게 된다.

        엣지 케이스:
            - 1페이지에서 완주: 페이지를 더 요청하지 않는다.
            - 중간 페이지의 형태 실패: 즉시 `RESPONSE_SHAPE` 실패. 받은 페이지를
              병합해 돌려주지 않는다.
            - 항목 0건 응답이 왔는데 아직 완주하지 않았다: `NO_PROGRESS` 실패.
            - 페이지 상한 도달: `PAGE_LIMIT` 실패. 조용히 멈추지 않는다.
            - 미지의 열거값: 경고 로그와 메타데이터에 남기고 **수집은 성공**이다.
        """
        stats = RequestStats()
        page_params = dict(params)
        if spec.family is not ResponseFamily.DOCUMENT:
            page_params["display"] = str(PAGE_SIZE)

        first = await self._fetch_with_stats(
            spec, page_params, stats, page=1 if spec.family is not ResponseFamily.DOCUMENT else None
        )
        if isinstance(first, CollectionFailure):
            return first
        if isinstance(first, ClassifiedFailure):
            return self._shape_failure(spec, first, stats)

        items: list[Element] = list(first.items)
        articles: list[Element] = list(first.articles)
        bodies: list[str] = [first.body]
        expected = first.total_count

        if expected is not None:
            page = 1
            while len(items) < expected:
                if page >= MAX_PAGES:
                    return CollectionFailure(
                        spec=spec,
                        reason=CollectionFailureReason.PAGE_LIMIT,
                        detail=(
                            f"{spec.key}: 페이지 상한 {MAX_PAGES}에 도달했다. "
                            f"totalCnt={expected}인데 {len(items)}건만 받았다. "
                            "조용히 멈추지 않고 실패로 올린다"
                        ),
                        stats=stats,
                        expected=expected,
                        received=len(items),
                    )
                page += 1
                nxt = await self._fetch_with_stats(spec, page_params, stats, page=page)
                if isinstance(nxt, CollectionFailure):
                    return nxt
                if isinstance(nxt, ClassifiedFailure):
                    return self._shape_failure(
                        spec, nxt, stats, expected=expected, received=len(items)
                    )
                if not nxt.items:
                    return CollectionFailure(
                        spec=spec,
                        reason=CollectionFailureReason.NO_PROGRESS,
                        detail=(
                            f"{spec.key}: {page}페이지가 항목 0건을 반환했다. "
                            f"totalCnt={expected}인데 {len(items)}건에서 진전이 없다"
                        ),
                        stats=stats,
                        expected=expected,
                        received=len(items),
                    )
                if len(nxt.items) < PAGE_SIZE and len(items) + len(nxt.items) < expected:
                    return CollectionFailure(
                        spec=spec,
                        reason=CollectionFailureReason.NO_PROGRESS,
                        detail=(
                            f"{spec.key}: {page}페이지가 {len(nxt.items)}건만 반환했다"
                            f"(display={PAGE_SIZE}). 마지막 페이지가 아닌데 페이지가 "
                            f"덜 찼으므로 API가 더 주지 않는다는 뜻이다 — "
                            f"totalCnt={expected}, 누적 {len(items) + len(nxt.items)}건. "
                            "여기서 멈추면 잘린 결과가 완주로 보인다"
                        ),
                        stats=stats,
                        expected=expected,
                        received=len(items) + len(nxt.items),
                    )
                items.extend(nxt.items)
                articles.extend(nxt.articles)
                bodies.append(nxt.body)

        report = check_integrity(spec, expected, items, articles)
        if not report.ok:
            _log.error(
                "law_api.integrity_failed",
                spec=spec.key,
                failures=[failure.value for failure in report.failures],
                detail=report.detail,
                collisions=[f"{key} x{count}" for key, count in report.collisions],
            )
            return CollectionFailure(
                spec=spec,
                reason=CollectionFailureReason.INCOMPLETE,
                detail=report.detail,
                stats=stats,
                expected=expected,
                received=len(items),
                report=report,
            )

        unknown = find_unknown_values(items)
        if unknown:
            _log.warning(
                "law_api.unknown_enum_values",
                spec=spec.key,
                values=unknown,
                message="관측 목록에 없는 열거값이다. 버리지 않고 기록한다 — "
                "닫힌 집합이 아니다 (law-api-spec.md §5.4)",
            )
        _log.info(
            "law_api.collected",
            spec=spec.key,
            requests=stats.requests,
            retries=stats.retries,
            items=len(items),
            articles=len(articles),
            total_count=expected,
            checked_keys=report.checked_keys,
            revision_groups=len(report.revision_groups),
        )
        return Collection(
            spec=spec,
            items=tuple(items),
            articles=tuple(articles),
            bodies=tuple(bodies),
            report=report,
            stats=stats,
            unknown_enum_values=unknown,
        )

    @staticmethod
    def _shape_failure(
        spec: TargetSpec,
        failure: ClassifiedFailure,
        stats: RequestStats,
        *,
        expected: int | None = None,
        received: int = 0,
    ) -> CollectionFailure:
        """응답 형태 실패를 수집 실패로 변환한다. 재시도하지 않는다."""
        _log.error(
            "law_api.response_shape_failed",
            spec=spec.key,
            kind=failure.kind.value,
            detail=failure.detail,
            excerpt=failure.body_excerpt,
        )
        return CollectionFailure(
            spec=spec,
            reason=CollectionFailureReason.RESPONSE_SHAPE,
            detail=failure.detail,
            stats=stats,
            expected=expected,
            received=received,
            response_kind=failure.kind,
        )
