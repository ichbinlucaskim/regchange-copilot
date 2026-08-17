"""응답 형태 분류가 실패를 0건으로 세지 않는지 고정한다 (R-11).

이 파일이 존재하는 이유: 법제처는 실패도 HTTP 200으로 주며 본문 형태로만 구별된다
(edge-case #10). 분류가 틀리면 수집 실패가 "그날 개정 없음"으로 위장하고, 담당자는
확인할 것이 없다고 잘못 안다 (ADR-005).
"""

from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import tostring

import pytest
from defusedxml.ElementTree import fromstring

from regchange.ingest.masking import MASK_PLACEHOLDER, Masker, MaskingError
from regchange.ingest.response import (
    EFLAW_DOCUMENT,
    EFLAW_SEARCH,
    JO_HISTORY_BY_ARTICLE,
    JO_HISTORY_BY_DATE,
    LAW_DOCUMENT,
    LAW_SEARCH,
    LS_HISTORY,
    SPECS,
    ClassifiedFailure,
    ClassifiedOk,
    ResponseFamily,
    ResponseKind,
    TargetSpec,
    classify,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"

TEST_CREDENTIAL = "test-oc-not-in-any-fixture"
"""픽스처는 이미 마스킹돼 있으므로, 실제 OC와 겹치지 않는 값을 쓴다."""


def masker() -> Masker:
    return Masker(TEST_CREDENTIAL)


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# 1. 에러·경계 픽스처 6종이 기대한 종으로 분류되는가
# ---------------------------------------------------------------------------

# 각 픽스처를 "그것이 실제로 어떤 요청의 응답이었는가"에 맞는 spec으로 분류한다.
# spec을 임의로 고르면 ROOT_MISMATCH가 먼저 걸려 다른 종을 검증하지 못한다.
ERROR_FIXTURE_CASES = [
    pytest.param(
        "error_search_zero_result.xml", LAW_SEARCH, ResponseKind.OK, id="검색계열_정상0건"
    ),
    pytest.param("error_law_bad_mst.xml", LAW_DOCUMENT, ResponseKind.LAW_MESSAGE, id="없는_MST"),
    pytest.param(
        "error_admrul_wrong_id_param.xml",
        LAW_DOCUMENT,
        ResponseKind.LAW_MESSAGE,
        id="행정규칙_잘못된_ID",
    ),
    pytest.param(
        "error_dayjochg_id_only_zero.xml",
        JO_HISTORY_BY_DATE,
        ResponseKind.OK,
        id="이력계열_파라미터부족_0건",
    ),
    pytest.param(
        "error_target_lsHistory.html", JO_HISTORY_BY_DATE, ResponseKind.HTML, id="미신청_target"
    ),
    pytest.param(
        "error_unknown_target_empty.xml",
        JO_HISTORY_BY_DATE,
        ResponseKind.EMPTY_BODY,
        id="알수없는_target_0바이트",
    ),
]


@pytest.mark.parametrize(("name", "spec", "expected"), ERROR_FIXTURE_CASES)
def test_error_fixtures_classify_as_expected(
    name: str, spec: TargetSpec, expected: ResponseKind
) -> None:
    assert classify(read(name), spec, masker=masker()).kind is expected


def test_empty_body_fixture_really_is_zero_bytes() -> None:
    # 파일 크기 0이 정상이라는 것이 픽스처 README의 서술이다. 그것을 고정한다.
    assert (FIXTURES / "error_unknown_target_empty.xml").stat().st_size == 0


# ---------------------------------------------------------------------------
# 2. 합성 입력으로만 만들 수 있는 두 종
# ---------------------------------------------------------------------------


def test_malformed_xml_is_parse_error() -> None:
    result = classify(b"<LawSearch><totalCnt>3</totalCnt>", JO_HISTORY_BY_DATE, masker=masker())
    assert result.kind is ResponseKind.PARSE_ERROR


def test_non_utf8_bytes_are_parse_error() -> None:
    # 선언을 보고 다른 인코딩으로 재시도하지 않는다 (edge-case #15).
    body = b'<?xml version="1.0" encoding="EUC-KR"?><LawSearch/>'
    result = classify(
        body.replace(b"<LawSearch/>", b"\xb9\xfd\xb7\xc9"), JO_HISTORY_BY_DATE, masker=masker()
    )
    assert result.kind is ResponseKind.PARSE_ERROR


def test_unexpected_root_is_root_mismatch() -> None:
    result = classify(
        b"<Unexpected><totalCnt>0</totalCnt></Unexpected>", LAW_SEARCH, masker=masker()
    )
    assert result.kind is ResponseKind.ROOT_MISMATCH


def test_history_without_total_count_is_parse_error() -> None:
    # 루트는 맞지만 계열 계약(totalCnt 존재)을 위반한다. 통과시키면 완주 검사가
    # 0을 기준으로 돌아 잘림을 놓친다.
    result = classify(
        b"<LawSearch><target>lsJoHstInf</target></LawSearch>", JO_HISTORY_BY_DATE, masker=masker()
    )
    assert result.kind is ResponseKind.PARSE_ERROR
    assert isinstance(result, ClassifiedFailure)
    assert "totalCnt" in result.detail


def test_history_with_non_numeric_total_count_is_parse_error() -> None:
    body = b"<LawSearch><target>lsJoHstInf</target><totalCnt>many</totalCnt></LawSearch>"
    assert classify(body, JO_HISTORY_BY_DATE, masker=masker()).kind is ResponseKind.PARSE_ERROR


def test_law_message_wins_over_root_mismatch() -> None:
    # <Law>는 어떤 spec에서도 기대 루트가 아니므로 ROOT_MISMATCH도 참이다.
    # 더 구체적인 진단(파라미터 오류)을 주는 LAW_MESSAGE가 이겨야 한다.
    result = classify(read("error_law_bad_mst.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert result.kind is ResponseKind.LAW_MESSAGE


# ---------------------------------------------------------------------------
# 3. 실증 — 같은 target이 endpoint에 따라 다른 스키마를 낸다
# ---------------------------------------------------------------------------


def test_by_article_response_under_by_date_spec_is_root_mismatch() -> None:
    """조문별 응답(루트 LawService)을 일자별 spec으로 분류하면 실패해야 한다."""
    result = classify(read("jochg_009244_jo000200_full.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert result.kind is ResponseKind.ROOT_MISMATCH
    assert isinstance(result, ClassifiedFailure)
    assert "LawService" in result.detail


def test_by_article_response_under_by_date_path_yields_zero_articles() -> None:
    """루트 대조가 없으면 조문이 조용히 0건이 된다 — 매핑표가 존재하는 이유.

    28건이 0건으로 보이며 예외도 경고도 없다. 이것이 이 저장소의 사건 기록
    3건과 같은 형태의 실패다 (누락 방향, 조용함).
    """
    root = fromstring(read("jochg_009244_jo000200_full.xml"))

    # 조문별 응답의 진짜 조문 경로
    assert len(root.findall(JO_HISTORY_BY_ARTICLE.article_path or "")) == 28
    # 일자별 경로로 세면 0건. 이것이 조용한 누락이다.
    assert len(root.findall(JO_HISTORY_BY_DATE.article_path or "")) == 0

    # 분류기가 이 상황을 애초에 막는다.
    assert (
        classify(read("jochg_009244_jo000200_full.xml"), JO_HISTORY_BY_DATE, masker=masker()).kind
        is ResponseKind.ROOT_MISMATCH
    )


def test_by_article_spec_reads_all_28_articles() -> None:
    result = classify(
        read("jochg_009244_jo000200_full.xml"), JO_HISTORY_BY_ARTICLE, masker=masker()
    )
    assert isinstance(result, ClassifiedOk)
    assert result.total_count == 28
    assert len(result.items) == 28
    assert len(result.articles) == 28


# ---------------------------------------------------------------------------
# 4. 이력 계열의 0건은 구별 불가능하다 (완료조건 2)
# ---------------------------------------------------------------------------


def test_history_zero_and_param_error_are_indistinguishable() -> None:
    """이력 계열에서 정상 0건과 파라미터 오류는 응답으로 구별할 수 없다.

    `error_dayjochg_id_only_zero.xml`은 `ID`만 주어 파라미터가 부족했던 응답이다.
    정상 0건 응답과 **바이트 단위로 같은 형태**이므로 분류가 둘 다 OK를 낸다.
    이것은 분류기의 결함이 아니라 API의 성질이며, 억지로 구별하려 들지 않는다.
    방어는 카나리아와 0건 재요청이 담당한다.
    """
    param_error = classify(
        read("error_dayjochg_id_only_zero.xml"), JO_HISTORY_BY_DATE, masker=masker()
    )
    genuine_zero = classify(
        b"<LawSearch><target>lsJoHstInf</target><totalCnt>0</totalCnt></LawSearch>",
        JO_HISTORY_BY_DATE,
        masker=masker(),
    )
    assert isinstance(param_error, ClassifiedOk)
    assert isinstance(genuine_zero, ClassifiedOk)
    assert param_error.kind is genuine_zero.kind is ResponseKind.OK
    assert param_error.total_count == genuine_zero.total_count == 0
    assert len(param_error.items) == len(genuine_zero.items) == 0


def test_search_family_zero_carries_result_code_but_that_is_not_a_success_signal() -> None:
    """검색 계열 0건에는 resultCode가 있지만, 그 차이는 계열 차이지 성공/실패 차이가 아니다."""
    search_zero = fromstring(read("error_search_zero_result.xml"))
    history_zero = fromstring(read("error_dayjochg_id_only_zero.xml"))
    assert search_zero.findtext("resultCode") == "00"
    assert history_zero.findtext("resultCode") is None
    # 그리고 이력 계열에는 resultCode가 구조적으로 없다 — 픽스처 전수로 확인.
    for name in ("dayjochg_regdt20250401.xml", "jochg_009244_jo000200_full.xml"):
        assert fromstring(read(name)).findtext("resultCode") is None


# ---------------------------------------------------------------------------
# 5. 계열 성질이 코드와 스펙에서 일치하는가
# ---------------------------------------------------------------------------

FAMILY_FIXTURES = [
    pytest.param("search_law_teukgeum.xml", LAW_SEARCH, 2, 2, id="검색_전건"),
    pytest.param("search_eflaw_teukgeum.xml", EFLAW_SEARCH, 81, 10, id="검색_잘림"),
    pytest.param("lschg_regdt20240719.xml", LS_HISTORY, 71, 5, id="이력_잘림"),
    pytest.param("dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, 83, 83, id="이력_전건"),
    pytest.param("jochg_009244_jo000200.xml", JO_HISTORY_BY_ARTICLE, 28, 20, id="조문별_잘림"),
    pytest.param("jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE, 28, 28, id="조문별_전건"),
]


@pytest.mark.parametrize(("name", "spec", "total", "items"), FAMILY_FIXTURES)
def test_list_fixtures_expose_total_and_item_counts(
    name: str, spec: TargetSpec, total: int, items: int
) -> None:
    result = classify(read(name), spec, masker=masker())
    assert isinstance(result, ClassifiedOk)
    assert result.total_count == total
    assert len(result.items) == items


def test_history_total_count_counts_laws_not_articles() -> None:
    """이력 계열의 totalCnt는 법령 수다. 조문 기준으로 완주 검사하면 항상 실패한다."""
    result = classify(read("dayjochg_regdt20250401.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert isinstance(result, ClassifiedOk)
    assert result.total_count == 83
    assert len(result.items) == 83
    assert len(result.articles) == 286  # 조문은 286건 — totalCnt와 다르다


DOCUMENT_FIXTURES = [
    pytest.param("law_009244_mst252787.xml", LAW_DOCUMENT, 34, id="특금법_본문"),
    pytest.param("law_010513_mst283193.xml", LAW_DOCUMENT, 682, id="자본시장법_본문"),
    pytest.param("eflaw_009256_mst261379_ef20240326.xml", EFLAW_DOCUMENT, 46, id="eflaw_본문"),
]


@pytest.mark.parametrize(("name", "spec", "units"), DOCUMENT_FIXTURES)
def test_document_family_has_no_total_count(name: str, spec: TargetSpec, units: int) -> None:
    result = classify(read(name), spec, masker=masker())
    assert isinstance(result, ClassifiedOk)
    assert result.total_count is None  # 완주 검사 대상이 아니라는 신호
    assert len(result.items) == units
    assert result.articles == ()


def test_document_family_is_not_total_count_expected() -> None:
    assert not LAW_DOCUMENT.total_count_expected
    assert not EFLAW_DOCUMENT.total_count_expected
    assert LAW_SEARCH.total_count_expected
    assert JO_HISTORY_BY_DATE.total_count_expected


def test_every_registered_spec_has_family_consistent_endpoint() -> None:
    """§2.1: lawSearch.do는 검색·이력, lawService.do는 본문·이력이다."""
    for spec in SPECS:
        if spec.family is ResponseFamily.SEARCH:
            assert spec.endpoint == "lawSearch.do"
        if spec.family is ResponseFamily.DOCUMENT:
            assert spec.endpoint == "lawService.do"
        # 이력 계열만 양쪽 endpoint에 존재한다.
        if spec.family is ResponseFamily.HISTORY:
            assert spec.endpoint in {"lawSearch.do", "lawService.do"}
            assert spec.article_path is not None


def test_same_target_differs_by_endpoint() -> None:
    """같은 lsJoHstInf가 루트 태그와 조문 경로 모두 다르다 (§2.1)."""
    assert JO_HISTORY_BY_DATE.target == JO_HISTORY_BY_ARTICLE.target == "lsJoHstInf"
    assert JO_HISTORY_BY_DATE.endpoint != JO_HISTORY_BY_ARTICLE.endpoint
    assert JO_HISTORY_BY_DATE.root_tag != JO_HISTORY_BY_ARTICLE.root_tag
    assert JO_HISTORY_BY_DATE.article_path != JO_HISTORY_BY_ARTICLE.article_path


# ---------------------------------------------------------------------------
# 6. 마스킹 — 분류 경로가 자격증명을 흘리지 않는가
# ---------------------------------------------------------------------------


def test_classify_masks_credential_in_body_and_excerpt() -> None:
    body = (
        f'<LawSearch><target>lsJoHstInf</target><totalCnt>1</totalCnt><law id="1">'
        f"<조문링크>/DRF/lawService.do?OC={TEST_CREDENTIAL}&amp;target=eflaw</조문링크>"
        f"</law></LawSearch>"
    ).encode()
    result = classify(body, JO_HISTORY_BY_DATE, masker=masker())
    assert isinstance(result, ClassifiedOk)
    assert TEST_CREDENTIAL not in result.body
    assert MASK_PLACEHOLDER in result.body
    # 파싱된 트리에도 남지 않는다 — 마스킹이 파싱보다 먼저다.
    assert TEST_CREDENTIAL not in tostring(result.root, encoding="unicode")


def test_classify_masks_credential_in_failure_excerpt() -> None:
    body = f"<html><body>OC={TEST_CREDENTIAL} 미신청</body></html>".encode()
    result = classify(body, JO_HISTORY_BY_DATE, masker=masker())
    assert isinstance(result, ClassifiedFailure)
    assert result.kind is ResponseKind.HTML
    assert TEST_CREDENTIAL not in result.body_excerpt


def test_empty_credential_is_rejected_at_construction() -> None:
    # 빈 문자열 치환은 본문을 파괴하면서 "마스킹 성공"으로 보인다.
    with pytest.raises(MaskingError):
        Masker("")
    with pytest.raises(MaskingError):
        Masker("   ")


def test_masker_rejects_body_where_credential_survives() -> None:
    # 치환 후에도 남는 경우를 강제로 만든다: 마스킹 토큰 안에 자격증명이 있는 형태.
    tricky = Masker(MASK_PLACEHOLDER[:1])
    with pytest.raises(MaskingError):
        tricky.mask(MASK_PLACEHOLDER)


def test_url_encoded_credential_is_also_masked() -> None:
    credential = "user@example.com"
    result = Masker(credential).mask(f"OC={credential}&x=1 그리고 OC=user%40example.com")
    assert credential not in result
    assert "user%40example.com" not in result


# ---------------------------------------------------------------------------
# 7. 전 픽스처 회귀 — 올바른 spec으로는 전부 OK여야 한다
# ---------------------------------------------------------------------------

CORRECT_SPEC_BY_FIXTURE = {
    "search_law_teukgeum.xml": LAW_SEARCH,
    "search_eflaw_teukgeum.xml": EFLAW_SEARCH,
    "lschg_regdt20240719.xml": LS_HISTORY,
    "dayjochg_regdt20200324.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20210105.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20230103.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20240109.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20250114.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20250401.xml": JO_HISTORY_BY_DATE,
    "dayjochg_regdt20260113.xml": JO_HISTORY_BY_DATE,
    "jochg_009244_jo000200.xml": JO_HISTORY_BY_ARTICLE,
    "jochg_009244_jo000200_full.xml": JO_HISTORY_BY_ARTICLE,
    "jochg_009244_jo000502.xml": JO_HISTORY_BY_ARTICLE,
    "law_009244_mst252787.xml": LAW_DOCUMENT,
    "law_009244_mst215971_v20200324.xml": LAW_DOCUMENT,
    "law_009244_mst113262_v20110519.xml": LAW_DOCUMENT,
    "law_010513_mst283193.xml": LAW_DOCUMENT,
    "law_001565_mst280405.xml": LAW_DOCUMENT,
    "law_010199_mst280277.xml": LAW_DOCUMENT,
    "law_001586_mst280373_tax.xml": LAW_DOCUMENT,
    "law_001638_mst281875_road.xml": LAW_DOCUMENT,
    "law_011357_mst270351_privacy.xml": LAW_DOCUMENT,
    "eflaw_009256_mst261379_ef20240326.xml": EFLAW_DOCUMENT,
    "eflaw_009256_mst261379_ef20240627.xml": EFLAW_DOCUMENT,
    "eflaw_009256_mst261379_ef20240719.xml": EFLAW_DOCUMENT,
    "eflaw_009244_mst283365_ef20260820_pending.xml": EFLAW_DOCUMENT,
}


@pytest.mark.parametrize(("name", "spec"), sorted(CORRECT_SPEC_BY_FIXTURE.items()))
def test_all_real_fixtures_classify_ok_under_correct_spec(name: str, spec: TargetSpec) -> None:
    result = classify(read(name), spec, masker=masker())
    assert isinstance(result, ClassifiedOk), getattr(result, "detail", "")


def test_fixture_spec_map_covers_every_non_error_fixture() -> None:
    """새 픽스처가 추가되면 이 매핑에 등록하도록 강제한다.

    등록을 강제하지 않으면 새 픽스처가 분류 회귀 검사를 받지 않고 지나간다 —
    "검사 대상에서 조용히 빠지는" 형태의 누락이다.
    """
    on_disk = {
        p.name
        for p in FIXTURES.glob("*.xml")
        if not p.name.startswith("error_") and not p.name.startswith("admrul")
        if "admrul" not in p.name
    }
    assert on_disk == set(CORRECT_SPEC_BY_FIXTURE), {
        "미등록": sorted(on_disk - set(CORRECT_SPEC_BY_FIXTURE)),
        "없는파일": sorted(set(CORRECT_SPEC_BY_FIXTURE) - on_disk),
    }


# ---------------------------------------------------------------------------
# 8. 루트 태그만으로는 target을 구별할 수 없다 — echo된 <target>으로 대조한다
# ---------------------------------------------------------------------------

SAME_SHAPE_SPECS = [LAW_SEARCH, EFLAW_SEARCH, LS_HISTORY, JO_HISTORY_BY_DATE]


def test_four_targets_share_root_tag_and_item_path() -> None:
    """네 target이 루트 태그와 항목 경로가 같다. 루트 대조만으로는 부족하다."""
    assert {s.root_tag for s in SAME_SHAPE_SPECS} == {"LawSearch"}
    assert {s.item_path for s in SAME_SHAPE_SPECS} == {"law"}
    # 그런데 target은 넷 다 다르고, 항목 내부의 식별자 경로도 갈린다.
    assert len({s.target for s in SAME_SHAPE_SPECS}) == 4
    assert LS_HISTORY.identity_path == "법령일련번호"
    assert JO_HISTORY_BY_DATE.identity_path == "법령정보/법령일련번호"


def test_echoed_target_mismatch_is_root_mismatch() -> None:
    """lsHstInf 응답을 lsJoHstInf spec으로 분류하면 실패해야 한다.

    루트 태그(LawSearch)와 항목 경로(law)가 같아 루트 대조는 통과한다.
    응답이 echo한 <target>을 대조하지 않으면 이 혼동이 통과하고, 그 뒤
    `법령정보/법령일련번호`가 전부 None이 되어 식별자가 조용히 사라진다.
    """
    result = classify(read("lschg_regdt20240719.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert result.kind is ResponseKind.ROOT_MISMATCH
    assert isinstance(result, ClassifiedFailure)
    assert "lsHstInf" in result.detail


def test_identity_path_silently_yields_none_under_wrong_spec() -> None:
    """echo 대조가 없으면 무엇이 조용히 사라지는가 — 식별자가 전부 None이 된다."""
    root = fromstring(read("lschg_regdt20240719.xml"))
    items = root.findall("law")
    assert len(items) == 5
    # 올바른 spec의 경로로는 5건 전부 식별자가 나온다.
    assert all(item.findtext(LS_HISTORY.identity_path or "") for item in items)
    # 잘못된 spec의 경로로는 5건 전부 None이다. 예외도 경고도 없다.
    assert all(item.findtext(JO_HISTORY_BY_DATE.identity_path or "") is None for item in items)


def test_eflaw_search_response_under_law_search_spec_is_root_mismatch() -> None:
    """law 검색과 eflaw 검색은 루트·항목·메타 필드가 전부 같다. target만 다르다."""
    result = classify(read("search_eflaw_teukgeum.xml"), LAW_SEARCH, masker=masker())
    assert result.kind is ResponseKind.ROOT_MISMATCH


def test_document_family_has_no_target_element_to_compare() -> None:
    """본문 응답에는 <target>이 없다. 그래서 이 계열은 echo 대조 대상이 아니다."""
    assert not LAW_DOCUMENT.target_echoed
    assert not EFLAW_DOCUMENT.target_echoed
    assert fromstring(read("law_009244_mst252787.xml")).findtext("target") is None
    for spec in SAME_SHAPE_SPECS:
        assert spec.target_echoed


# ---------------------------------------------------------------------------
# 9. 식별키가 계열별로 확정되었는가 (edge-case #18)
# ---------------------------------------------------------------------------


def test_history_specs_declare_different_version_time_fields() -> None:
    """두 이력 계열이 서로 다른 시점 필드를 쓴다. 보편 키는 존재하지 않는다."""
    assert JO_HISTORY_BY_DATE.version_time_path == "조문시행일"
    assert JO_HISTORY_BY_ARTICLE.version_time_path == "조문변경일"
    assert JO_HISTORY_BY_DATE.article_no_path == JO_HISTORY_BY_ARTICLE.article_no_path == "조문번호"


def test_declared_identity_fields_exist_in_real_responses() -> None:
    """선언한 경로가 실제 응답에 존재하는지 확인한다 — 스펙과 코드의 두 벌 일치."""
    by_date = classify(read("dayjochg_regdt20250401.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert isinstance(by_date, ClassifiedOk)
    for item in by_date.items:
        assert item.findtext(JO_HISTORY_BY_DATE.identity_path or "")
    for jo in by_date.articles:
        assert jo.findtext(JO_HISTORY_BY_DATE.article_no_path or "")
        assert jo.findtext(JO_HISTORY_BY_DATE.version_time_path or "")

    by_article = classify(
        read("jochg_009244_jo000200_full.xml"), JO_HISTORY_BY_ARTICLE, masker=masker()
    )
    assert isinstance(by_article, ClassifiedOk)
    for jo in by_article.articles:
        assert jo.findtext(JO_HISTORY_BY_ARTICLE.article_no_path or "")
        assert jo.findtext(JO_HISTORY_BY_ARTICLE.version_time_path or "")


def test_by_date_version_time_field_absent_in_by_article_schema() -> None:
    """계열을 섞으면 시점 필드가 None이 된다 — 보편 키를 만들면 안 되는 이유."""
    by_article = fromstring(read("jochg_009244_jo000200_full.xml"))
    articles = by_article.findall(JO_HISTORY_BY_ARTICLE.article_path or "")
    assert len(articles) == 28
    # 조문별 스키마에는 조문시행일이 없다.
    assert all(a.findtext("조문시행일") is None for a in articles)
    assert all(a.findtext("조문변경일") for a in articles)


def test_mst_alone_is_not_an_identity_and_the_extra_rows_are_effective_dates() -> None:
    """edge-case #18: MST 3쌍은 중복이 아니라 조문변경일이 다른 시행일 버전이다.

    MST만 키로 쓰면 3건이 사라진다. 그 3건이 valid_from 이며 ADR-005 가 이력 API를
    진입점으로 삼은 이유다. 이 테스트가 깨지는 것은 키에서 시점 필드를 뺐다는 뜻이다.
    """
    result = classify(
        read("jochg_009244_jo000200_full.xml"), JO_HISTORY_BY_ARTICLE, masker=masker()
    )
    assert isinstance(result, ClassifiedOk)

    rows = []
    for law in result.items:
        jo = law.find(JO_HISTORY_BY_ARTICLE.article_path.split("/", 1)[1])  # type: ignore[union-attr]
        assert jo is not None
        rows.append(
            (
                law.findtext(JO_HISTORY_BY_ARTICLE.identity_path or ""),
                jo.findtext("조문번호"),
                jo.findtext(JO_HISTORY_BY_ARTICLE.version_time_path or ""),
            )
        )

    assert len(rows) == 28
    # 확정된 식별키로는 충돌 0건.
    assert len(set(rows)) == 28
    # 시점 필드를 빼면 3건이 조용히 사라진다.
    without_time = {(mst, no) for mst, no, _ in rows}
    assert len(without_time) == 25
    assert len(rows) - len(without_time) == 3


def test_by_date_identity_holds_across_the_largest_fixture() -> None:
    """581행짜리 최대 픽스처에서도 확정된 키가 충돌하지 않는다."""
    result = classify(read("dayjochg_regdt20210105.xml"), JO_HISTORY_BY_DATE, masker=masker())
    assert isinstance(result, ClassifiedOk)
    rows = []
    for law in result.items:
        mst = law.findtext(JO_HISTORY_BY_DATE.identity_path or "")
        for jo in law.findall("조문정보/jo"):
            rows.append((mst, jo.findtext("조문번호"), jo.findtext("조문시행일")))
    assert len(rows) == 2335
    assert len(set(rows)) == len(rows)
