"""법령 XML 파서를 픽스처로 채점하고, 파싱 규칙 위반을 회귀 테스트로 고정한다.

이 테스트가 존재하는 이유: 조문키 재구성이라는 **정답이 픽스처에 이미 있다.**
파서가 틀리면 인용이 가리키는 대상이 바뀌므로(ADR-001, ADR-007), 정답이 있는 동안
1.00을 고정해 둔다. 그리고 edge-case #4(목 귀속)처럼 **인용 검증을 통과해 버리는**
실패 유형은 여기서만 잡을 수 있다.
"""

import datetime as dt
from pathlib import Path

import pytest

from regchange.parse import ParseError, parse_law_document
from regchange.parse.models import MarkerType, MoveKind, UnitType
from regchange.parse.normalize import NORM_RULE_VERSION, normalize
from regchange.parse.references import parse_move_references

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
LAW_FILES = sorted(FIXTURES.glob("law_*.xml")) + sorted(FIXTURES.glob("eflaw_*.xml"))
TEUKGEUM_2020 = FIXTURES / "law_009244_mst215971_v20200324.xml"
TEUKGEUM_NOW = FIXTURES / "law_009244_mst252787.xml"
CAPITAL_MARKETS = FIXTURES / "law_010513_mst283193.xml"
ROAD_TRAFFIC = FIXTURES / "law_001638_mst281875_road.xml"


def test_fixture_set_is_not_empty() -> None:
    """픽스처가 사라졌는데 아래 채점이 0건 통과하는 것을 막는다."""
    assert len(LAW_FILES) >= 13


# --- 채점: 조문키 재구성 -------------------------------------------------------


@pytest.mark.parametrize("path", LAW_FILES, ids=lambda p: p.name)
def test_article_key_reconstruction_is_exact(path: Path) -> None:
    """(article_no, branch_no, unit_type)로 재구성한 조문키가 원본과 일치한다."""
    doc = parse_law_document(path)
    mismatched = [u.article_key for u in doc.units if u.reconstructed_key() != u.article_key]
    assert not mismatched, f"{path.name}: 재구성 불일치 {mismatched[:5]}"


def test_overall_parser_accuracy_is_one() -> None:
    """픽스처 전체 조문단위에 대한 정확도가 1.00이다."""
    total = matched = 0
    for path in LAW_FILES:
        for unit in parse_law_document(path).units:
            total += 1
            matched += unit.reconstructed_key() == unit.article_key
    assert total > 1900, f"조문단위 수가 예상보다 적다: {total}"
    assert matched == total, f"정확도 {matched / total:.4f} — 1.00이어야 한다"


# --- 구조: 목 귀속 (edge-case #4) ----------------------------------------------


def test_mok_belongs_to_preceding_ho_by_document_order() -> None:
    """목은 호의 자식이 아니라 항의 자식이며, 문서 순서로 직전 호에 귀속된다."""
    doc = parse_law_document(TEUKGEUM_NOW)
    article2 = next(u for u in doc.units if u.article_key == "0002001")
    hang = article2.hangs[0]
    counts = [len(ho.moks) for ho in hang.hos]
    assert counts[:6] == [15, 4, 0, 3, 3, 0], f"제2조 호별 목 개수가 다르다: {counts[:6]}"
    assert hang.hos[0].moks[0].num == "가."
    assert hang.hos[0].moks[-1].num == "거.", "하. 다음은 거.다 (edge-case #17)"
    assert hang.hos[1].moks[0].num == "가.", "호가 바뀌면 목번호가 리셋된다"


def test_no_mok_appears_without_a_preceding_ho() -> None:
    """픽스처 전체에서 '선행 호 없는 목'이 0건이다. 문서 순서 귀속의 전제다."""
    for path in LAW_FILES:
        parse_law_document(path)  # 위반이면 ParseError가 난다


def test_mok_without_preceding_ho_raises() -> None:
    """선행 호 없이 목이 나오면 조용히 넘기지 않고 예외를 던진다."""
    xml = """<법령><기본정보><법령ID>000001</법령ID></기본정보><조문>
    <조문단위 조문키="0001001"><조문번호>1</조문번호><조문여부>조문</조문여부>
    <조문내용>제1조(x)</조문내용>
    <항><항번호>①</항번호><항내용>a</항내용>
    <목><목번호>가.</목번호><목내용>b</목내용></목></항>
    </조문단위></조문></법령>"""
    with pytest.raises(ParseError, match="선행 <호> 없이"):
        parse_law_document(xml)


def test_seq_in_doc_is_unique_and_ordered() -> None:
    """순서를 잃는 자료구조를 쓰지 않았음을 고정한다 (ADR-003 정정 이력)."""
    doc = parse_law_document(CAPITAL_MARKETS)
    seqs = [u.seq_in_doc for u in doc.units]
    assert seqs == sorted(seqs) == list(range(len(seqs)))


def test_duplicate_article_key_is_preserved_not_collapsed() -> None:
    """조문키가 중복돼도 행이 사라지지 않는다. 자본시장법 0011000은 3행이다."""
    doc = parse_law_document(CAPITAL_MARKETS)
    same = [u for u in doc.units if u.article_key == "0011000"]
    assert len(same) == 3, "제목행이 서로를 덮어썼다 (edge-case #2)"
    assert all(u.unit_type is UnitType.HEADING for u in same)


# --- 구조: 조문내용의 조건부 의미 (edge-case #5) --------------------------------


def test_article_content_is_full_text_when_no_hang() -> None:
    """항이 없는 조문의 조문내용에는 전문이 들어간다."""
    doc = parse_law_document(TEUKGEUM_NOW)
    article1 = next(u for u in doc.units if u.article_key == "0001001")
    assert not article1.hangs
    assert "목적으로 한다" in article1.content.raw


def test_article_content_is_title_only_when_hangs_exist() -> None:
    """항이 있는 조문의 조문내용에는 제목 줄만 들어간다."""
    doc = parse_law_document(TEUKGEUM_NOW)
    article = next(u for u in doc.units if u.article_key == "0004021")
    assert article.hangs
    assert article.content.raw.strip() == "제4조의2(금융회사등의 고액 현금거래 보고)"
    assert "금융회사등은" in article.hangs[0].text.raw


def test_empty_shell_hang_is_accepted() -> None:
    """항 구분 없이 호만 있는 조문에는 항번호 없는 껍데기 항이 삽입된다."""
    doc = parse_law_document(TEUKGEUM_NOW)
    article2 = next(u for u in doc.units if u.article_key == "0002001")
    assert article2.hangs[0].num is None
    assert article2.hangs[0].text.raw.strip() == ""
    assert article2.hangs[0].hos


# --- 구조: 편장절관 계층 (edge-case #9) ----------------------------------------


def test_heading_path_is_reconstructed_from_text() -> None:
    """편/장/절/관 계층을 제목행 텍스트에서 스택으로 재구성한다."""
    doc = parse_law_document(CAPITAL_MARKETS)
    article = next(u for u in doc.articles if u.heading_path and len(u.heading_path) >= 3)
    kinds = [p.split()[0][-1] for p in article.heading_path]
    assert kinds[0] == "편"
    assert len(article.heading_path) == len(set(article.heading_path))


# --- 정규화 norm-v2 ------------------------------------------------------------


def test_norm_version_is_v2() -> None:
    """규칙을 바꿨으므로 버전을 올렸다 (ADR-002)."""
    assert NORM_RULE_VERSION == "norm-v2"
    assert normalize("x").rule_version == "norm-v2"


def test_keyword_markers_are_removed_and_extracted() -> None:
    """`<개정 …>`·`<신설 …>`은 제거하고 마커로 추출한다."""
    result = normalize("이 법은 … 한다. <개정 2014.3.24, 2020.2.4, 2023.3.14>")
    assert "<개정" not in result.norm
    assert result.markers[0].type is MarkerType.AMENDED
    assert len(result.markers[0].dates) == 3, "복수 날짜 나열을 전제한다"


def test_bare_date_marker_is_removed() -> None:
    """키워드 없는 날짜 마커도 제거한다. `② 삭제 <2013.8.13>` 형태 (norm-v2)."""
    result = normalize("② 삭제 <2013.8.13>")
    assert result.norm == "삭제"
    assert result.markers[0].type is MarkerType.UNKNOWN_DATE


def test_non_date_angle_tokens_are_preserved() -> None:
    """날짜가 없으면 마커가 아니다 — `<54>`와 `<img …>`는 그대로 둔다."""
    hang = normalize("① 부터 <54> 까지 생략")
    assert "<54>" in hang.norm, "항번호 15 이상의 대체 표기다"
    img = normalize('별표 <img id="126840219"></img>')
    assert "<img" in img.norm, "img id는 호출 간 안정적이므로 보존한다"


def test_proviso_marker_is_extracted_but_kept() -> None:
    """`<단서 생략>`은 날짜가 없어 제거하지 않고 추출만 한다."""
    result = normalize("이 법은 공포한 날부터 시행한다. <단서 생략>")
    assert "<단서 생략>" in result.norm
    assert result.markers[0].type is MarkerType.PROVISO_OMITTED


def test_number_prefix_is_stripped() -> None:
    """번호 접두어를 제거한다. 번호는 이미 별도 필드에 있다 (edge-case #6)."""
    assert normalize('1. "금융회사등"이란 …').norm.startswith('"금융회사등"')
    assert normalize("가. 「한국산업은행법」에 따른").norm.startswith("「한국산업은행법」")
    assert normalize("① 금융회사등은").norm.startswith("금융회사등은")


def test_hanja_and_brackets_are_not_altered() -> None:
    """규칙 4 — 한자·괄호·인용부호를 변형하지 않는다."""
    text = "징역과 벌금을 병과(竝科)할 수 있다. 「법률명」 제2조"
    assert normalize(text, strip_prefix=False).norm == text


def test_empty_text_normalizes_without_error() -> None:
    """빈 껍데기 항의 항내용은 실제로 빈 문자열이다. 예외를 던지지 않는다."""
    assert normalize("").norm == ""


# --- 조문참고자료의 이동 표기 ---------------------------------------------------


def test_explicit_moves_resolve_the_punishment_ambiguity() -> None:
    """벌칙 2:2 모호성이 명시 표기로 해소된다 (ADR-003 근거 정정)."""
    doc = parse_law_document(TEUKGEUM_2020)
    sources = {
        u.article_key: m.source.render()
        for u in doc.units
        for m in u.moves
        if m.kind is MoveKind.MOVED_FROM and m.source is not None
    }
    assert sources["0016001"] == "제13조"
    assert sources["0017001"] == "제14조"
    assert len(sources) == 15, "가지번호 조문 3건을 포함해 15건이다"


def test_branch_article_moves_are_captured() -> None:
    """제목 매칭이 놓쳤던 가지번호 조문 이동을 명시 표기는 잡는다."""
    doc = parse_law_document(TEUKGEUM_2020)
    sources = {
        u.article_key: m.source.render()
        for u in doc.units
        for m in u.moves
        if m.kind is MoveKind.MOVED_FROM and m.source is not None
    }
    assert sources["0010021"] == "제7조의2"
    assert sources["0012021"] == "제9조의2"
    assert sources["0015021"] == "제11조의2"


def test_move_reference_preserves_raw() -> None:
    """텍스트에서 추출한 것이므로 원문을 보존한다 (ADR-007과 같은 지위)."""
    doc = parse_law_document(TEUKGEUM_2020)
    move = next(m for u in doc.units for m in u.moves)
    assert move.raw.startswith("[") and "이동" in move.raw


def test_departure_version_has_no_move_markers() -> None:
    """이동 표기는 도착 버전에만 있다 (ADR-003 근거 b — 양방향 검증 불가)."""
    doc = parse_law_document(FIXTURES / "law_009244_mst113262_v20110519.xml")
    assert not [m for u in doc.units for m in u.moves]


def test_particle_variants_are_matched() -> None:
    """조사 변형(는/은, 로/으로, 종전의)을 모두 매칭한다."""
    cases = [
        "[종전 제9조는 제12조로 이동 <2020.3.24>]",
        "[종전 제10조의8은 제10조의9로 이동 <2021.3.23>]",
        "[종전 제81조의8은 제81조의10으로 이동 <2006.12.30>]",
        "[종전의 제81조의3은 제81조의4로 이동 <2006.12.30>]",
    ]
    for case in cases:
        refs = parse_move_references(case)
        assert refs, f"매칭 실패: {case}"
        assert refs[0].kind is MoveKind.PREVIOUS_MOVED_TO


def test_combined_bracket_yields_two_references() -> None:
    """한 대괄호의 두 표기를 각각 별도 참조로 낸다."""
    refs = parse_move_references("[제6조에서 이동, 종전 제9조는 제12조로 이동 <2020.3.24>]")
    kinds = {r.kind for r in refs}
    assert kinds == {MoveKind.MOVED_FROM, MoveKind.PREVIOUS_MOVED_TO}


def test_previous_deleted_is_distinguished_from_move() -> None:
    """`종전 제34조의2는 삭제`는 이동이 아니라 삭제다."""
    refs = parse_move_references("[제39조의10에서 이동, 종전 제34조의2는 삭제 <2023.3.14>]")
    assert {r.kind for r in refs} == {MoveKind.MOVED_FROM, MoveKind.PREVIOUS_DELETED}


def test_false_positive_movement_phrase_is_rejected() -> None:
    """'이동'이 단어로 들어간 문장을 이동 표기로 오인하지 않는다."""
    text = "[약물의 영향으로 … 자동차등(개인형 이동장치는 제외한다) … 이하 이 항에서 같다]"
    assert parse_move_references(text) == ()


def test_road_traffic_fixture_has_no_false_move() -> None:
    """도로교통법 픽스처에서 오탐이 생기지 않는다."""
    doc = parse_law_document(ROAD_TRAFFIC)
    assert not [m for u in doc.units for m in u.moves]


# --- 실패를 드러내는 경로 -------------------------------------------------------


def test_article_key_length_mismatch_raises() -> None:
    """조문키 길이가 7이 아니면 조용히 잘라내지 않고 예외를 던진다."""
    xml = """<법령><기본정보><법령ID>000001</법령ID></기본정보><조문>
    <조문단위 조문키="00010"><조문번호>1</조문번호><조문여부>조문</조문여부>
    <조문내용>x</조문내용></조문단위></조문></법령>"""
    with pytest.raises(ParseError, match="조문키 길이"):
        parse_law_document(xml)


def test_unknown_type_digit_raises() -> None:
    """유형 자리가 0/1이 아니면 기본값을 넣지 않고 예외를 던진다 (ADR-001)."""
    xml = """<법령><기본정보><법령ID>000001</법령ID></기본정보><조문>
    <조문단위 조문키="0001009"><조문번호>1</조문번호><조문여부>조문</조문여부>
    <조문내용>x</조문내용></조문단위></조문></법령>"""
    with pytest.raises(ParseError, match="유형 자리"):
        parse_law_document(xml)


def test_admrul_root_is_rejected() -> None:
    """행정규칙은 이 파서의 대상이 아니다 (ADR-006, 3단계)."""
    with pytest.raises(ParseError, match="행정규칙"):
        parse_law_document(FIXTURES / "admrul_2100000267264.xml")


def test_zero_articles_is_failure_not_success() -> None:
    """조문 0건 수집은 성공이 아니라 실패다 (ADR-005)."""
    xml = "<법령><기본정보><법령ID>000001</법령ID></기본정보><조문></조문></법령>"
    with pytest.raises(ParseError, match="0건"):
        parse_law_document(xml)


def test_ministry_code_is_read_from_the_xml_attribute() -> None:
    """소관부처코드는 자식 태그가 아니라 `<소관부처>`의 속성이다 (edge-case #20).

    태그만 순회하는 파서는 이 값을 놓치고, 그러면 부처 해결이 이름으로 넘어간다.
    이름은 조인 키가 아니다 (ADR-009). 값이 비는 것이 아니라 다른 키로 조용히
    이동하므로, 속성에서 읽는다는 사실을 테스트로 고정한다.
    """
    document = parse_law_document(FIXTURES / "law_009244_mst252787.xml")
    assert document.ministry == "금융위원회"
    assert document.ministry_code == "1160100"


def test_missing_ministry_element_yields_none_not_empty_string() -> None:
    """`<소관부처>`가 없으면 None 이다. 빈 문자열을 부처 코드로 흘리지 않는다."""
    xml = """<법령><기본정보><법령ID>000001</법령ID></기본정보><조문>
    <조문단위 조문키="0001001"><조문번호>1</조문번호><조문여부>조문</조문여부>
    <조문내용>제1조 목적</조문내용></조문단위></조문></법령>"""
    document = parse_law_document(xml)
    assert document.ministry is None
    assert document.ministry_code is None


def test_promulgation_number_is_read() -> None:
    """공포번호를 읽는다. 같은 날 두 번 공포된 사례가 실측 48건 있다 (edge-case #18)."""
    document = parse_law_document(FIXTURES / "law_009244_mst215971_v20200324.xml")
    assert document.promulgation_date == dt.date(2020, 3, 24)
    assert document.promulgation_no == "17113"


def test_article_effective_date_string_is_kept_raw_not_parsed() -> None:
    """`조문시행일자문자열`을 원문 그대로 보관한다. 해석하지 않는다.

    입도가 조문 단위보다 잘고(항/호/단서) 자유 텍스트라, 파싱하면 ADR-007 의 PARSED
    등급이 된다. 그 등급의 값을 bitemporal 의 핵심 축에 넣을 수 없다.
    """
    document = parse_law_document(FIXTURES / "law_010199_mst280277.xml")

    assert document.article_effective_dates_raw == "20251216:제42조제3항,제42조의2제1항"
    assert document.has_article_level_effective_dates is True


def test_body_api_carries_article_level_effective_dates_that_tags_flatten() -> None:
    """**조문별 시행일 분기가 본문 API 에 있는데 조문 단위 태그에는 없다** (edge-case #8 정정).

    이 테스트가 고정하는 것: `<조문시행일자>` 태그는 문서 시행일자로 평탄화되지만,
    `<기본정보><조문시행일자문자열>` 은 분기를 요약 문자열로 담는다는 사실.
    "본문에서는 분기가 관측되지 않는다"는 서술이 부정확했고, 그 부정확이
    valid_from 결정의 전제였다.
    """
    document = parse_law_document(FIXTURES / "law_010199_mst280277.xml")

    effective_dates = {unit.effective_date for unit in document.articles}
    assert effective_dates == {dt.date(2026, 12, 17)}, "조문 단위 태그는 평탄화되어 있다"
    assert document.document_effective_date == dt.date(2026, 12, 17)

    # 그런데 요약 문자열은 다른 날짜를 담고 있다 — 분기가 존재한다는 증거다.
    assert document.article_effective_dates_raw is not None
    assert "20251216" in document.article_effective_dates_raw
    assert "20261217" not in document.article_effective_dates_raw


def test_document_without_the_string_reports_no_article_level_divergence() -> None:
    """필드가 없으면 분기 없음으로 읽는다. 파싱 없이 얻는 유일한 신호다."""
    document = parse_law_document(FIXTURES / "law_009244_mst215971_v20200324.xml")

    assert document.article_effective_dates_raw is None
    assert document.has_article_level_effective_dates is False
