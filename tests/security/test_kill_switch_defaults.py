"""킬 스위치의 기본값이 꺼짐(fail-closed)인지, 그리고 **환경변수가 스위치를 켤 수 없는지**
검사한다.

이 테스트가 막는 위협:
    - 신규 환경 기동이나 설정 누락이 LLM 호출·검색·외부 발송을 아무도 모르게 활성화하는 것.
    - **효과 없는 설정이 켜진 상태로 읽히는 것** (2026-08-21 이후의 위협). 스위치가
      환경변수에서 DB 로 옮겨졌으므로(ADR-019), `.env` 에 `LLM_ENABLED=true` 를 적어도
      아무 일도 일어나지 않는다. 그 줄이 파일에 남아 있으면 운영자는 켜져 있다고 읽고,
      **실제로는 꺼져 있는데 켜졌다고 믿는 상태**가 된다. 반대 방향(꺼졌다고 믿는데
      켜져 있음)보다 덜 위험하지만, 둘 다 "설정과 사실이 다르다"는 같은 실패다.

**정정 이력 (2026-08-21)**: 이 파일은 원래 `.env.example` 의 `LLM_ENABLED=false` 등을
검사했다. 그 검사는 스위치가 환경변수였을 때 옳았고, 지금은 반대를 검사한다 —
**선언이 없어야 한다.** 파일을 지우지 않고 검사 대상을 바꾼 이유는 CLAUDE.md §6 이고,
더 실질적으로는 이 파일이 사라지면 "기본값이 꺼짐"을 아무도 검사하지 않게 되기 때문이다.
값의 기본값 자체는 `test_kill_switches.py::test_default_is_off_for_every_switch` 가
게이트 수준에서 검사한다.
"""

from pathlib import Path

import pytest

from regchange.guards.killswitch import Switch

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.split("#")[0].strip()
    return values


@pytest.mark.security
def test_env_example_exists() -> None:
    """.env.example 이 사라지면 아래 검사가 조용히 무의미해진다."""
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} 가 없다"


@pytest.mark.security
@pytest.mark.parametrize("switch", [s.value for s in Switch])
def test_env_example_does_not_declare_switches(switch: str) -> None:
    """환경변수로 스위치를 선언하지 않는다. **효과 없는 설정은 거짓말이다.**"""
    values = _env_example_values()

    assert switch not in values, (
        f"{switch} 가 .env.example 에 선언돼 있다. 스위치는 DB 에 있다 (ADR-019) — "
        "이 줄은 아무 효과가 없으면서 켜진 상태로 읽힌다"
    )


@pytest.mark.security
def test_env_example_points_to_the_switch_command() -> None:
    """어디서 켜는지를 파일이 알려 준다. 선언만 지우면 켜는 방법을 잃는다."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "regchange switch" in text
    assert "kill_switch" in text


@pytest.mark.security
def test_no_secret_values_committed_in_env_example() -> None:
    """.env.example 에 실제 자격증명이 들어가는 사고를 막는다."""
    values = _env_example_values()

    assert values.get("LLM_API_KEY", "") == "", "LLM_API_KEY 에 값이 들어 있다"
    assert values.get("LAW_GO_KR_OC", "") == "", "LAW_GO_KR_OC 에 값이 들어 있다"
