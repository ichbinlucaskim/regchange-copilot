"""킬 스위치 — 검색·LLM 호출·발송을 재배포 없이 멈춘다 (CLAUDE.md §6).

목적:
    기능 하나를 "지금 해도 되는가"로 판정하고, 안 되면 **멈춘다.** 그리고 멈춘 이유를
    호출부가 구별할 수 있게 한다 — 꺼져서 안 한 것과 읽지 못해서 못 한 것은 다른 사실이다.

「멈춘다」의 정의:
    **예외를 던진다.** 빈 결과나 `None` 을 돌려주지 않는다.

    빈 결과로 멈추면 그 빈 값이 하류로 흐르고, 검색 0건은 "인용할 문단이 없다"로,
    영향 0건은 "영향 없음"으로 읽힌다. **스위치가 담당자에게 '괜찮다'로 보인다.**
    이 저장소는 같은 판단을 이미 두 번 했다 — `INSUFFICIENT_EVIDENCE` 를 "없다"가 아니라
    "모른다"로 정의한 것, 카나리아 실패를 성공한 0건과 구별해 `SKIPPED_CANARY_FAILED`
    로 남긴 것.

    호출부는 예외를 잡아 기록하거나 전파한다. **잡아서 기본값으로 대체하지 않는다.**

멈춘 이유를 셋으로 구별한다:
    | 종류 | 무슨 일이 있었나 | 조치 |
    |---|---|---|
    | `OFF` | 누가 껐다. 이유와 사람이 행에 있다 | 그 사람에게 묻는다 |
    | `NEVER_SET` | 아무도 켠 적이 없다 (행 자체가 없다) | 켠다. **기본값은 꺼짐이다** |
    | `UNAVAILABLE` | 상태를 읽지 못했다 (DB 장애 등) | 장애 대응. 스위치 문제가 아니다 |

    셋 다 멈추지만 조치가 다르다. 하나로 뭉치면 DB 장애가 운영자의 결정처럼 보인다.

구현 이유:
    **fail-closed 다. 모르면 안 한다.** 상태를 읽지 못했을 때 계속 도는 쪽이 편해 보이는
    순간이 반드시 오지만, **그 순간이 정확히 스위치가 필요한 순간이다** — 무언가 잘못돼서
    멈추려는데 그 무언가 때문에 스위치를 못 읽는 상황. 그때 "모르니까 계속"이면 스위치가
    없는 것과 같다.

    **캐시는 게이트에 있고 어댑터에 없다.** 어댑터 Protocol 은 "지금 값을 읽어 온다"만
    약속한다 (`adapters/switches.py`). SSM 처럼 자체 캐싱 특성이 다른 구현으로 갈아끼울 때
    인터페이스가 캐시를 규정하고 있으면 그 특성과 싸우게 된다.

    **성공한 조회만 캐시한다.** 실패를 캐시하면 DB 가 돌아온 뒤에도 최대 60초 더 멈춘다 —
    장애 시간을 우리가 늘리는 셈이다. 대신 장애 중에는 매 호출이 접속을 시도한다
    (접속 타임아웃 3초, `store/dsn.py`).

트레이드오프:
    - **반영이 즉시가 아니라 최대 60초다.** "60초 내 반영"은 상한이지 보장이 아니다.
      즉시 반영이 필요하면 프로세스를 재시작한다 (런북). 캐시 무효화 경로는 **지금
      만들지 않는다** — 실제로 60초가 문제가 되는지 아직 모르고, 안 만든 것을 나중에
      만드는 것이 만든 것을 걷어내는 것보다 싸다.
    - 게이트 객체를 조립 시점에 만들어 들고 다녀야 한다. 전역 싱글턴으로 두면 테스트가
      실제 DB 를 공유하게 되고, 스위치를 검사하는 테스트가 스위치 상태에 좌우된다.
    - 검사 지점이 호출 경로마다 있다. 한 곳에서만 검사하면 새 진입점이 생겼을 때
      조용히 빠지고, 그 사실은 스위치를 실제로 꺼 보기 전까지 드러나지 않는다.

엣지 케이스:
    - **진행 중인 호출**: 중단하지 않는다. 검사는 노드/함수 **진입에서** 하며, 이미 나간
      모델 호출은 끝까지 간다. 중간에 끊으면 `llm_invocation` 행이 남지 않는 호출이
      생기고, 그것은 "기록되지 않은 호출"이라 이 저장소가 더 비싸게 치는 실패다.
    - **승인 대기 중인 그래프**: `LLM_ENABLED` 를 꺼도 재개된다. 승인 이후 노드는 모델을
      부르지 않기 때문이다 — 사람이 이미 본 것에 대한 결정은 계속 기록될 수 있어야 한다.
      발송은 별개이며 `DISPATCH_ENABLED` 가 막는다 (승인은 승인이고 발송은 발송이다).
    - **같은 게이트를 동시에 여러 코루틴이 씀**: 캐시가 dict 이고 갱신이 원자적이지 않아
      같은 스위치를 두 번 읽을 수 있다. 읽기 두 번은 무해하므로 락을 두지 않는다.
    - **시계**: `time.monotonic` 을 쓴다. 벽시계는 NTP 보정으로 뒤로 갈 수 있고, 그러면
      캐시가 예상보다 오래 산다.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from regchange.adapters.switches import SwitchState, SwitchStore

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
"""캐시 수명. **"재배포 없이 60초 내 반영" 요건에서 곧바로 나온 값이다.**

더 짧게 하면 조회가 늘고 얻는 것이 없다(60초 요건을 이미 만족한다). 더 길게 하면
요건을 어긴다. 즉 이 값은 취향이 아니라 요건 그 자체이며, 바꾸려면 요건을 먼저 바꾼다."""


class Switch(StrEnum):
    """멈출 수 있는 기능 3종. 값은 `kill_switch.name` 의 CHECK 와 같다."""

    RETRIEVAL = "RETRIEVAL_ENABLED"
    """사내 규정 검색. 끄면 후보 문단을 만들지 않는다."""
    LLM = "LLM_ENABLED"
    """모델 호출 전부 — 추출·초안·검증. 끄면 프롬프트가 조립되지 않는다."""
    DISPATCH = "DISPATCH_ENABLED"
    """승인된 건의 외부 발송. 끄면 승인은 기록되고 발송만 멈춘다."""


class StopKind(StrEnum):
    """왜 멈췄는가. 조치가 다르므로 값이 다르다."""

    OFF = "OFF"
    """누가 껐다. 이유와 사람이 `kill_switch` 행에 있다."""
    NEVER_SET = "NEVER_SET"
    """아무도 켠 적이 없다. 기본값은 꺼짐이다 (fail-closed)."""
    UNAVAILABLE = "UNAVAILABLE"
    """상태를 읽지 못했다. 스위치 문제가 아니라 장애다."""


class KillSwitchError(RuntimeError):
    """기능이 멈췄다. 하위 예외가 이유를 구별한다.

    호출부가 `except KillSwitchError` 으로 "멈췄다"를 한 번에 잡을 수 있고, `kind` 로
    무엇 때문인지 구별한다. 잡아서 **기본값으로 대체하지 않는다** — 그러면 스위치가
    "영향 없음"으로 위장한다.
    """

    def __init__(self, switch: Switch, kind: StopKind, detail: str) -> None:
        """멈춘 기능과 이유를 함께 담는다. 메시지만으로 조치가 정해져야 한다."""
        self.switch = switch
        self.kind = kind
        self.detail = detail
        super().__init__(f"{switch.value} 가 꺼져 있다 ({kind.value}): {detail}")


class SwitchOffError(KillSwitchError):
    """누가 껐다."""

    def __init__(self, switch: Switch, state: SwitchState) -> None:
        """누가 언제 왜 껐는지를 메시지에 그대로 싣는다."""
        self.state = state
        super().__init__(
            switch,
            StopKind.OFF,
            f"{state.changed_by} 가 {state.changed_at:%Y-%m-%d %H:%M %Z} 에 껐다 — {state.reason}",
        )


class SwitchNeverSetError(KillSwitchError):
    """아무도 켠 적이 없다. 설정 누락이 기능 활성으로 이어지지 않는다."""

    def __init__(self, switch: Switch) -> None:
        """켜는 방법을 메시지에 적는다 — 이 예외를 처음 보는 사람이 다음 행동을 알아야 한다."""
        super().__init__(
            switch,
            StopKind.NEVER_SET,
            "설정된 적이 없다. 기본값은 꺼짐이다 — `regchange switch on` 으로 켠다",
        )


class SwitchUnknownError(KillSwitchError):
    """상태를 읽지 못했다 — **꺼진 것과 다른 사실이다**.

    조치가 다르다: 이쪽은 장애 대응이고 저쪽은 껐다는 사람에게 묻는 일이다.
    """

    def __init__(self, switch: Switch, cause: Exception) -> None:
        """원인 예외를 보존한다. 장애 대응은 그 예외를 봐야 시작된다."""
        self.cause = cause
        super().__init__(switch, StopKind.UNAVAILABLE, f"상태를 읽지 못했다: {cause!r}")


@dataclass(slots=True)
class _Cached:
    state: SwitchState | None
    expires_at: float


@dataclass(slots=True)
class SwitchGate:
    """스위치를 읽고 판정한다. 캐시가 여기 있다.

    목적:
        "이 기능을 지금 해도 되는가"에 답하고, 안 되면 이유를 구별해 예외를 던진다.

    구현 이유:
        게이트를 **객체**로 둔다. 함수로 두면 캐시가 전역이 되고, 전역 캐시는 테스트가
        실제 DB 상태를 공유하게 만든다 — 스위치를 검사하는 테스트가 스위치 상태에
        좌우되면 그 테스트는 아무것도 보증하지 않는다.

        `store` 를 주입받는다. 테스트는 `StaticSwitchStore` 를 주고, 운영은
        `PostgresSwitchStore` 를 준다. **이것은 우회 플래그가 아니라 어댑터 경계다** —
        검사 로직은 하나이고 값의 출처만 다르다.

    트레이드오프:
        모듈 docstring 참조.

    엣지 케이스:
        모듈 docstring 참조.
    """

    store: SwitchStore
    ttl_seconds: float = CACHE_TTL_SECONDS
    _cache: dict[Switch, _Cached] = field(default_factory=dict, repr=False)

    async def state(self, switch: Switch) -> SwitchState | None:
        """현재 값을 돌려준다(캐시 최대 `ttl_seconds` 초). 판정하지 않는다."""
        now = time.monotonic()
        cached = self._cache.get(switch)
        if cached is not None and cached.expires_at > now:
            return cached.state
        try:
            state = await self.store.read(switch.value)
        except Exception as exc:  # 어떤 조회 실패든 멈춤으로 수렴시킨다 (fail-closed)
            raise SwitchUnknownError(switch, exc) from exc
        # 성공한 조회만 캐시한다. 실패를 캐시하면 복구된 뒤에도 더 멈춘다.
        self._cache[switch] = _Cached(state=state, expires_at=now + self.ttl_seconds)
        return state

    async def require(self, switch: Switch) -> SwitchState:
        """켜져 있으면 그 상태를, 아니면 **멈춘다**(예외).

        반환값을 쓰지 않아도 된다. 호출 자체가 관문이며, 반환값은 "무엇을 근거로
        진행했는가"를 기록하고 싶은 호출부를 위한 것이다.
        """
        state = await self.state(switch)
        if state is None:
            raise SwitchNeverSetError(switch)
        if not state.enabled:
            raise SwitchOffError(switch, state)
        return state

    def invalidate(self) -> None:
        """캐시를 비운다. **운영 경로에서 부르지 않는다** — 테스트와 CLI 확인용이다."""
        self._cache.clear()


@dataclass(frozen=True, slots=True)
class StaticSwitchStore:
    """고정된 값을 돌려주는 저장소다 (**테스트와 eval 러너 전용**).

    목적:
        DB 없이 스위치 상태를 정하고 게이트의 판정을 시험한다.

    구현 이유:
        `guards` 안에 둔다. 테스트 디렉터리에 두면 eval 러너가 쓸 수 없고, eval 러너는
        실제 DB 를 쓰되 스위치까지 켜 두기를 요구하면 측정이 운영 설정에 좌우된다.

        **이것은 승인 우회 플래그와 다르다.** 검사 로직을 건너뛰는 것이 아니라 값의
        출처를 바꾼다 — 꺼진 값을 주면 게이트는 똑같이 멈춘다. 실제로 `tests/security`
        의 발화 테스트가 이 저장소로 스위치를 꺼서 "멈추는가"를 검사한다.

    트레이드오프:
        운영 코드 옆에 테스트용 구현이 있다. 분리하면 import 경로가 길어지고, 무엇보다
        **이 클래스가 운영에서 쓰이면 안 된다는 사실이 덜 보인다.**

    엣지 케이스:
        - 등록되지 않은 스위치: `None` — 즉 `NEVER_SET`. 기본값이 꺼짐인 것을 테스트가
          자연스럽게 상속한다.
    """

    states: dict[str, SwitchState]

    async def read(self, name: str) -> SwitchState | None:
        """등록된 값을 그대로 돌려준다."""
        return self.states.get(name)


def static_gate(
    values: Mapping[Switch, bool],
    *,
    changed_by: str = "test-fixture",
    reason: str = "고정값 — 테스트/측정 실행",
) -> SwitchGate:
    """고정된 스위치 값으로 게이트를 만든다 (**테스트와 eval 러너 전용**).

    목적:
        DB 상태와 무관하게 "켜진 상태"/"꺼진 상태"를 만들어 판정을 시험한다.

    구현 이유:
        `changed_by` 를 `test-fixture` 로 기본 지정한다. 운영 값처럼 보이는 문자열을
        기본값으로 두면 로그에서 시험 실행과 운영 실행이 구별되지 않는다.

    트레이드오프:
        `values` 에 없는 스위치는 `NEVER_SET` 이 된다. 편의를 위해 "나머지는 켜짐"으로
        두지 않는다 — 기본값이 켜짐인 헬퍼가 있으면 그것이 곧 우회 도구가 된다.

    엣지 케이스:
        - 빈 매핑: 모든 스위치가 `NEVER_SET` 이다. 그 자체가 유효한 시험 조건이다.
    """
    changed_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return SwitchGate(
        store=StaticSwitchStore(
            states={
                switch.value: SwitchState(
                    name=switch.value,
                    enabled=enabled,
                    changed_by=changed_by,
                    reason=reason,
                    changed_at=changed_at,
                )
                for switch, enabled in values.items()
            }
        )
    )
