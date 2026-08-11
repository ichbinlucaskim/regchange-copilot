"""픽스처에 API 자격증명(OC)이 남아 있지 않은지 검사한다.

이 테스트가 막는 위협: 법제처 Open API 응답 본문의 `법령상세링크`, `조문링크`,
`조문변경이력상세링크` 필드가 요청 URL을 통째로 담고 있어 `OC=<발급값>`이 그대로
echo된다. 응답을 그대로 저장하면 자격증명이 저장소에 커밋된다.

`OC=`의 값이 마스킹 토큰 `***`가 아닌 픽스처가 하나라도 있으면 실패한다.
.env 없이도 동작하므로 CI에서 항상 검사된다.

CLAUDE.md §6에 따라 이 파일은 리팩터링 편의를 이유로 삭제하거나 skip 하지 않는다.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "law_api"

# 응답 본문에 echo되는 형태: OC=값&target=... 또는 OC=값" 등
OC_PARAM = re.compile(r"OC=([^&\"'<>\s]*)")

MASK = "***"

FIXTURES = sorted(FIXTURE_DIR.glob("*")) if FIXTURE_DIR.is_dir() else []


def test_fixture_directory_is_populated() -> None:
    """픽스처가 사라졌는데 아래 검사가 0건 통과하는 것을 막는다."""
    assert FIXTURES, f"{FIXTURE_DIR} 에 픽스처가 없다"


@pytest.mark.security
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_fixture_has_no_unmasked_oc(fixture: Path) -> None:
    """모든 픽스처의 OC 파라미터 값은 마스킹 토큰이어야 한다."""
    if fixture.is_dir():
        pytest.skip("디렉터리")

    content = fixture.read_text(encoding="utf-8", errors="replace")
    leaked = {value for value in OC_PARAM.findall(content) if value != MASK}

    assert not leaked, (
        f"{fixture.name}: OC 값이 마스킹되지 않았다. "
        f"발견된 값 개수={len(leaked)} (값 자체는 출력하지 않는다)"
    )
