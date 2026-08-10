"""킬 스위치의 기본값이 꺼짐(fail-closed)인지 검사한다.

이 테스트가 막는 위협: 신규 환경 기동이나 설정 누락이 LLM 호출·검색·외부 발송을
아무도 모르게 활성화하는 것. `.env.example` 은 `make setup` 이 그대로 복사해
`.env` 를 만드는 원본이므로, 여기서 켜진 값이 곧 새 환경의 기본 상태가 된다.

CLAUDE.md §6 에 따라 이 파일은 리팩터링 편의를 이유로 삭제하거나 skip 하지 않는다.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

KILL_SWITCHES = ("LLM_ENABLED", "RETRIEVAL_ENABLED", "DISPATCH_ENABLED")


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
@pytest.mark.parametrize("switch", KILL_SWITCHES)
def test_kill_switch_defaults_to_off(switch: str) -> None:
    """모든 킬 스위치는 .env.example 에서 명시적으로 false 여야 한다."""
    values = _env_example_values()

    assert switch in values, f"{switch} 가 .env.example 에 선언되어 있지 않다"
    assert values[switch] == "false", (
        f"{switch} 의 기본값이 {values[switch]!r} 이다. fail-closed 여야 한다 (CLAUDE.md §6)"
    )


@pytest.mark.security
def test_no_secret_values_committed_in_env_example() -> None:
    """.env.example 에 실제 자격증명이 들어가는 사고를 막는다."""
    values = _env_example_values()

    assert values.get("LLM_API_KEY", "") == "", "LLM_API_KEY 에 값이 들어 있다"
    assert values.get("LAW_GO_KR_OC", "") == "", "LAW_GO_KR_OC 에 값이 들어 있다"
