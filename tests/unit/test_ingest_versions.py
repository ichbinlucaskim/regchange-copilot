"""직전 MST 판정 — 실제 응답 픽스처와, 조용히 틀릴 수 있는 형태들.

이 테스트가 존재하는 이유: `parse_previous_version` 이 틀리면 **모든 diff 가 틀린 짝으로
계산되고 결과는 그럴듯해 보인다.** 예외도 경고도 나지 않는다. 특히 제정본 응답의
`구조문_기본정보/법령일련번호` 는 비어 있지 않고 `"0"` 이라서, 빈 값 검사만으로는
통과해 버리고 그 값이 MST 로 하류에 흘러간다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regchange.ingest.versions import (
    PreviousVersion,
    VersionResolutionError,
    parse_previous_version,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"

WITH_PREVIOUS = "oldandnew_000030_mst285199.xml"
CHAINED = "oldandnew_000030_mst283843.xml"
ENACTED = "oldandnew_288527_enacted.xml"
ADMRUL = "admrul_oldandnew_efsv_2100000282622.xml"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_previous_mst_is_read_from_the_old_block() -> None:
    """실측 표본 1 — 285199 의 직전은 283843 이다."""
    result = parse_previous_version(read(WITH_PREVIOUS), requested_mst="285199")

    assert isinstance(result, PreviousVersion)
    assert result.previous.mst == "283843"
    assert result.previous.law_id == "000030"
    assert result.previous.promulgation_date == "20260310"
    assert result.previous.promulgation_no == "21445"
    assert result.previous.revision_kind == "타법개정"
    assert result.requested.mst == "285199"


def test_chain_holds_on_the_second_sample() -> None:
    """실측 표본 2 — 283843 의 직전은 282481 이다.

    표본이 하나면 "직전"인지 "임의의 이전 버전"인지 구별되지 않는다. 정보통신망법
    2026년 공포 순서(282481 → 283843 → 285199)와 맞춰 두 단계를 확인한다.
    """
    result = parse_previous_version(read(CHAINED), requested_mst="283843")

    assert isinstance(result, PreviousVersion)
    assert result.previous.mst == "282481"
    assert result.previous.promulgation_date == "20260106"


def test_enacted_law_returns_none_not_the_sentinel_mst() -> None:
    """제정본은 `None` 이다. **`"0"` 을 MST 로 돌려주지 않는다.**

    이것이 이 모듈이 막는 조용한 실패의 본체다. 응답에 `구조문_기본정보` 가 존재하고
    `법령일련번호` 가 비어 있지도 않다 — `"0"` 이다.
    """
    assert parse_previous_version(read(ENACTED), requested_mst="288527") is None


def test_sentinel_value_is_actually_present_in_the_fixture() -> None:
    """픽스처가 정말 sentinel 형태인지 확인한다.

    위 테스트가 다른 이유로 통과하는 것을 막는다 — 픽스처가 바뀌어 `구조문_기본정보`
    자체가 사라지면 `None` 은 여전히 나오지만 우리가 시험하려던 것은 사라진다.
    """
    body = read(ENACTED)
    assert "<신구법존재여부>N</신구법존재여부>" in body
    assert "<법령일련번호>0</법령일련번호>" in body


def test_requested_mst_must_match_the_new_block() -> None:
    """응답의 신조문 MST 가 요청과 다르면 예외.

    API 가 다른 버전의 직전을 돌려주면 그 시점부터 모든 diff 가 틀린 짝이 된다.
    """
    with pytest.raises(VersionResolutionError, match="요청과 다르다"):
        parse_previous_version(read(WITH_PREVIOUS), requested_mst="999999")


def test_admrul_response_is_rejected_by_root_tag() -> None:
    """행정규칙 신구법 응답을 법령 파서에 넣으면 예외.

    루트가 `AdmRulOldAndNewService` 로 갈리므로 잡힌다. 두 응답은 자식 구조가
    똑같아서(§6.3.2) 루트를 보지 않으면 그대로 파싱되고, 행정규칙일련번호가
    법령 MST 자리에 들어간다.
    """
    with pytest.raises(VersionResolutionError, match="루트 태그"):
        parse_previous_version(read(ADMRUL), requested_mst="2100000282622")


def test_sentinel_without_marker_is_an_error_not_none() -> None:
    """sentinel 인데 `신구법존재여부` 가 없으면 예외 — `None` 이 아니다.

    두 신호가 어긋나는 형태는 관측된 적이 없다. 어느 쪽을 믿을 근거가 없으므로
    "직전 없음"으로 해석하지 않는다.
    """
    body = read(ENACTED).replace("<신구법존재여부>N</신구법존재여부>", "")

    with pytest.raises(VersionResolutionError, match="조용히 실패"):
        parse_previous_version(body, requested_mst="288527")


def test_unknown_marker_value_is_an_error() -> None:
    """`신구법존재여부` 가 `N` 이 아닌 값이면 예외. 미지의 값을 정상으로 읽지 않는다."""
    body = read(ENACTED).replace(
        "<신구법존재여부>N</신구법존재여부>", "<신구법존재여부>Y</신구법존재여부>"
    )

    with pytest.raises(VersionResolutionError, match="관측된 값은"):
        parse_previous_version(body, requested_mst="288527")


def test_marker_without_sentinel_is_an_error() -> None:
    """`신구법존재여부=N` 인데 구조문에 실제 MST 가 있으면 예외."""
    body = read(WITH_PREVIOUS).replace(
        "</신조문_기본정보>", "</신조문_기본정보><신구법존재여부>N</신구법존재여부>"
    )

    with pytest.raises(VersionResolutionError, match="어긋나며"):
        parse_previous_version(body, requested_mst="285199")


def test_malformed_xml_is_an_error() -> None:
    """XML 이 깨지면 예외. `None` 으로 뭉개지 않는다."""
    with pytest.raises(VersionResolutionError, match="XML 파싱 실패"):
        parse_previous_version("<OldAndNewService><깨짐>", requested_mst="285199")


def test_missing_new_block_is_an_error() -> None:
    """`신조문_기본정보` 가 없으면 예외. 요청 대조 자체가 불가능하다."""
    body = "<OldAndNewService><구조문_기본정보/></OldAndNewService>"

    with pytest.raises(VersionResolutionError, match="신조문_기본정보"):
        parse_previous_version(body, requested_mst="285199")
