"""발송 킬 스위치 관문 — **워커가 첫 줄에서 이것을 부른다** (원칙 5, 5단계).

목적:
    `DISPATCH_ENABLED` 가 꺼져 있으면 발송 경로가 시작되지 않게 한다.

구현 이유:
    **워커보다 관문을 먼저 만든다.** 이 저장소는 `dispatch/` 에 대해 같은 순서를 이미
    한 번 썼다 — 코드가 없는 상태에서 import 계약(`pyproject.toml`)과 DB 권한(003)을
    먼저 걸었다. 계약이 코드보다 먼저 있으면 위반이 생길 수 없다.

    **`ops/switches.py` 를 import 하지 않는다.** 그쪽은 쓰기 경로이며, 발송 워커가
    스위치를 켤 수 있게 되는 순간 "잠깐 켜고 보내자"가 가능해진다. 워커는 읽기만 한다 —
    DB 권한이 이미 그렇게 되어 있고(013), 코드도 그 모양이어야 한다.

    **승인과 발송을 구별한다.** 이 스위치가 꺼져도 승인은 기록되고 `action_outbox` 행은
    쌓인다. 담당자의 판단을 막을 이유가 없기 때문이다 — 막는 것은 **외부로 나가는 일**
    하나다. 켜면 쌓인 것이 나간다.

트레이드오프:
    - **지금 이 관문이 막을 대상이 없다.** 발송 워커가 아직 없으므로, 이 스위치의 시험은
      「관문이 멈추는가」까지이고 「워커가 실제로 안 보내는가」가 아니다. 그 한계를 여기와
      `tests/security/test_kill_switches.py` 에 적어 둔다 — 적어 두지 않으면 6단계에
      "스위치는 이미 검증됐다"로 읽힌다.
    - 함수 하나짜리 모듈이다. 워커 파일에 함께 두면 파일이 하나 적지만, **워커가 생기기
      전에 계약이 있어야** 한다는 것이 요지이므로 분리했다.

엣지 케이스:
    - 스위치를 읽지 못함: `SwitchUnknownError` 로 멈춘다. 발송은 되돌릴 수 없으므로
      "모르면 보낸다"가 성립할 수 없다 (fail-closed).
    - 진행 중인 발송: 이 관문은 시작을 막을 뿐 진행 중인 외부 호출을 끊지 않는다.
      끊으면 "보냈는지 모르는 상태"가 생기고, 그것이 중복 발송의 원인이다.
"""

from __future__ import annotations

import logging

from regchange.adapters.switches import SwitchState
from regchange.guards.killswitch import Switch, SwitchGate

logger = logging.getLogger(__name__)


async def require_dispatch_enabled(switches: SwitchGate) -> SwitchState:
    """발송을 시작해도 되는지 묻고, 아니면 멈춘다(예외).

    목적:
        발송 워커의 유일한 시작 관문.

    구현 이유:
        반환값으로 스위치 상태를 준다. 워커가 "무엇을 근거로 보냈는가"를 실행 기록에
        남길 수 있어야 하며, 그 근거는 승인 레코드와 **이 스위치 상태** 둘이다.

    트레이드오프:
        워커가 이 함수를 부르지 않으면 관문이 없는 것과 같다. 호출을 강제할 방법은
        코드에 없고, `tests/security` 가 그것을 검사한다 — 워커가 생기는 시점에
        그 테스트도 함께 커진다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    state = await switches.require(Switch.DISPATCH)
    logger.info("dispatch 관문 통과: %s 가 켠 상태 — %s", state.changed_by, state.reason)
    return state
