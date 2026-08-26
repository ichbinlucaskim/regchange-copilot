"""적대적 세트가 **스키마를 지키고 적재 경로에 닿지 않는가**.

이 테스트가 존재하는 이유: 이 디렉터리의 파일은 프롬프트 인젝션 문자열을 담고 있다.
사내 규정 코퍼스로 적재되면 검색 결과에 섞이고, 그 순간부터 `trusted` 등급을 달고
프롬프트에 들어간다 — R-23 이 막은 바로 그 경로다. 파일 이름과 위치가 1차 방어이고,
이 테스트가 그 방어를 고정한다.

스키마를 검사하는 이유는 다르다. `path` 나 `violation_check` 가 빠지면 러너가 그
케이스를 조용히 다르게 다루고, **측정이 무엇을 쟀는지 알 수 없게 된다.**
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ADVERSARIAL_DIR = REPO_ROOT / "evals" / "datasets" / "adversarial"
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
CORPUS_DIR = REPO_ROOT / "evals" / "corpus" / "internal-policies"

REQUIRED = ("id", "based_on", "injection_type", "injection_location", "path", "injected_text")
PATHS = {"EXTRACTION_ONLY", "FULL"}
LOCATIONS = {"MIDDLE", "END", "TITLE_ADJACENT", "INSIDE_PARAGRAPH"}
CHECKS = {"MARKER", "STATUS_FLIP", "FABRICATED_CITATION", "QUOTE_TAMPERED"}

MIN_CASES, MAX_CASES = 10, 20
"""세트 크기. 상한을 15 → 20 으로 올렸다 (2026-08-23).

**유도 강도 2단계 4건을 더했기 때문이다.** 1단계(평서문)에서 날조 시도가 0건이라
「폐기 0건이 (a)인가 (b)인가」를 가르지 못했고, 분모가 0인 지표는 지표가 아니다.
상한을 올린 것은 세트를 키우려는 것이 아니라 **강도 축을 하나 더 시험하기 위해서**이며,
3단계까지 가지 않는다 — 무한정 강화하면 "언젠가는 뚫린다"만 확인하게 된다."""
MIN_LOCATIONS = 3
"""삽입 위치 최소 종수. **3종을 요구하는 이유가 실측으로 확인됐다** —
`system:` 마커를 항 안쪽(`INSIDE_PARAGRAPH`)에 넣자 줄 시작 패턴이 놓쳤다.
위치를 한 종으로만 시험했다면 그 미탐을 못 봤다."""


def _cases() -> list[dict[str, object]]:
    return [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(ADVERSARIAL_DIR.glob("adv-*.yaml"))
    ]


def test_case_count_is_in_range() -> None:
    """10~15건. 적으면 유형을 못 덮고, 많으면 예산을 넘는다."""
    assert MIN_CASES <= len(_cases()) <= MAX_CASES


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["id"]))
def test_required_fields(case: dict[str, object]) -> None:
    """필수 필드가 빠지면 러너가 그 케이스를 조용히 다르게 다룬다."""
    for key in REQUIRED:
        assert key in case, f"{case.get('id')}: {key} 없음"
    assert case["path"] in PATHS
    assert case["injection_location"] in LOCATIONS
    assert case["violation_check"] in CHECKS
    expected = case["expected"]
    assert isinstance(expected, dict)
    assert set(expected) >= {"scanner_fires", "instruction_followed"}


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["id"]))
def test_based_on_points_at_a_real_golden_case(case: dict[str, object]) -> None:
    """원천이 실재해야 "원문은 이렇고 우리가 무엇을 심었는가"가 성립한다."""
    assert (GOLDEN_DIR / f"{case['based_on']}.yaml").is_file()


def test_at_least_three_injection_locations() -> None:
    """위치를 3종 이상 시험한다 — 같은 문자열도 위치가 바뀌면 탐지가 뒤집힌다."""
    locations = {str(c["injection_location"]) for c in _cases()}
    assert len(locations) >= MIN_LOCATIONS, f"위치 {len(locations)}종: {sorted(locations)}"


def test_full_path_is_reserved_for_gate_two_cases() -> None:
    """**전 경로는 gate 2단 겨냥 케이스만.** 나머지를 전 경로로 돌리면 비용이 3배가 된다.

    지시 추종은 첫 LLM 호출에서 드러나므로 격리 시험은 추출까지면 충분하다.
    """
    for case in _cases():
        if case["path"] == "FULL":
            assert case["violation_check"] in {"FABRICATED_CITATION", "QUOTE_TAMPERED"}, (
                f"{case['id']}: gate 2단 겨냥이 아닌데 전 경로다"
            )


def test_marker_cases_declare_a_canary() -> None:
    """카나리아 없이 `MARKER` 로 두면 「따랐는가」를 기계가 판정할 수 없다."""
    for case in _cases():
        if case["violation_check"] == "MARKER":
            assert case.get("canary"), f"{case['id']}: canary 없음"


def test_fixtures_are_not_loadable_as_policy_corpus() -> None:
    """**적재 경로에 닿지 않는다.** 로더는 `ISP-*.md` 만 읽는다.

    파일 이름과 위치가 1차 방어다 — 코드를 고치지 않아도 글롭이 먼저 걸러낸다.
    """
    assert not list(ADVERSARIAL_DIR.glob("*.md")) or all(
        p.name == "README.md" for p in ADVERSARIAL_DIR.glob("*.md")
    )
    assert not list(ADVERSARIAL_DIR.glob("ISP-*")), "코퍼스 로더 패턴과 겹치는 파일이 있다"
    assert ADVERSARIAL_DIR.resolve() != CORPUS_DIR.resolve()
    assert not str(ADVERSARIAL_DIR.resolve()).startswith(str(CORPUS_DIR.resolve()))


def test_injected_text_never_reaches_the_corpus_directory() -> None:
    """코퍼스 문서에 카나리아 문자열이 섞이지 않았는지 본다.

    적재 경로가 막혀 있어도 **누가 손으로 복사할 수 있다.** 그 사고를 여기서 잡는다.
    """
    canaries = [str(c["canary"]) for c in _cases() if c.get("canary")]
    for path in CORPUS_DIR.glob("ISP-*.md"):
        text = path.read_text(encoding="utf-8")
        for canary in canaries:
            assert canary not in text, f"{path.name} 에 적대적 카나리아가 들어갔다"


def test_readme_warns_about_the_content() -> None:
    """README 가 이 파일들이 무엇인지 경고한다. 경고 없이 두면 다음 사람이 그냥 읽는다."""
    text = (ADVERSARIAL_DIR / "README.md").read_text(encoding="utf-8")
    assert "프롬프트 인젝션 문자열을 포함한다" in text
    assert "적재하지 마라" in text
