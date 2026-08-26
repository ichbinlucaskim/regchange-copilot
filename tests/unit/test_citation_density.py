"""인용 밀도 대조(`docs/21` §7)의 **정의를 고정한다.**

이 테스트가 존재하는 이유: §7의 판정(개정 0.3750 vs 사내 0.1447)이 전적으로 「인용을
무엇으로 세는가」에 걸려 있다. 정의가 조용히 움직이면 두 수의 비교가 무효가 되는데,
결과 파일에는 그 변화가 드러나지 않는다. 정의를 여기에 박아 둔다.

`measure_text()` 와 `verdict()` 는 I/O 가 없는 순수 함수라 DB·스냅샷 없이 고정된다.
"""

from __future__ import annotations

import pytest
from evals.runners.citation_density import measure_text, verdict

INTERNAL_DOC_REF = "「정보보호 관리지침」(ISP-GUIDE-002) 제35조에 따라 점검한다"
LAW_NAME_ONLY = "「정보통신망 이용촉진 및 정보보호 등에 관한 법률」에 따른 침해사고"
DETACHED_REF = "「은행법」에 따른 은행(같은 법 제59조에 따라 설립된 것을 말한다)"


def test_adjacent_law_name_and_article_is_other_law() -> None:
    """`「법령명」` 직후 공백 1자 뒤의 조 표기가 타법 인용의 형태다."""
    got = measure_text("t", '"전기통신금융사기"란 「전기통신기본법」 제2조제1호에 따른', None)
    assert got.other_law == frozenset({"제2조"})
    assert got.internal == frozenset()


def test_law_name_without_article_counts_nowhere() -> None:
    """`docs/09` §5.3 이 「법령명만」을 조 단위 인용과 별도 칸으로 센 것과 같다."""
    got = measure_text("t", LAW_NAME_ONLY, None)
    assert got.other_law == frozenset()
    assert got.internal == frozenset()


def test_internal_document_name_is_not_a_law() -> None:
    """접미사가 「지침」이라 법령이 아니고, `(문서ID)` 때문에 간격도 1자를 넘는다."""
    got = measure_text("t", INTERNAL_DOC_REF, None)
    assert got.other_law == frozenset()
    assert got.internal == frozenset({"제35조"})


def test_detached_law_reference_leaks_into_internal() -> None:
    """**알려진 오분류다** (`measure_text` 트레이드오프).

    방향이 타법 인용을 깎는 쪽이라 「개정 쪽이 더 인용한다」는 결론에 불리하고, 그래서
    허용했다. 이 테스트는 그 사실이 조용히 바뀌지 않도록 고정한다 — 고치려면 §7 을
    다시 재야 한다.
    """
    got = measure_text("t", DETACHED_REF, None)
    assert got.other_law == frozenset()
    assert got.internal == frozenset({"제59조"})


def test_self_article_number_is_excluded() -> None:
    """조립본 머리표기 때문에 빼지 않으면 전건이 자기 자신을 인용한 것이 된다."""
    got = measure_text("t", "제1조(목적) 이 법은 제5조에 따른 조치를 정한다", "제1조")
    assert got.internal == frozenset({"제5조"})


def test_policy_paragraph_has_no_heading_to_exclude() -> None:
    """`policy_paragraph.text_raw` 에는 머리표기가 없다 — 개정 조문과의 비대칭이다."""
    got = measure_text("t", "③ 관리체계의 범위는 제13조에 따른 정보자산 목록을 기준으로 한다", None)
    assert got.internal == frozenset({"제13조"})


def test_other_law_and_internal_are_not_exclusive() -> None:
    """둘 다 하는 조가 실제로 있다. 배타 분류하면 세 비율 중 하나를 잃는다."""
    got = measure_text("t", "「개인정보 보호법」 제18조와 이 규정 제7조에 따른다", None)
    assert got.other_law == frozenset({"제18조"})
    assert got.internal == frozenset({"제7조"})


def test_empty_body_does_not_raise() -> None:
    """본문이 비었다는 것은 파서가 아니라 원문의 사실일 수 있다."""
    got = measure_text("t", "", None)
    assert got.other_law == frozenset()
    assert got.internal == frozenset()


@pytest.mark.parametrize(
    ("amended", "expected"),
    [
        (0.1447, "SIMILAR"),
        (0.2200, "SIMILAR"),  # 차 0.0753 < 판정 폭 0.0876
        (0.3750, "AMENDMENT_HIGHER"),  # 실측값
        (0.0300, "AMENDMENT_LOWER"),
    ],
)
def test_verdict_splits_at_two_standard_errors(amended: float, expected: str) -> None:
    """판정 폭은 측정 전에 고정했다 (`docs/21` §7.4). 152/112 에서 ±0.0876."""
    got = verdict(0.1447, amended, 152, 112)
    assert got["band"] == pytest.approx(0.0876, abs=0.0001)
    assert got["verdict"] == expected
