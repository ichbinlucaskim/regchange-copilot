"""잘림과 식별키 충돌이 실패로 처리되는지 고정한다 (사건 2, edge-case #11/#18).

이 파일이 존재하는 이유: 응답에 "잘렸다"는 표시가 없으므로 `totalCnt` 대조가 유일한
검출 수단이다. 이 저장소는 그 대조를 빼먹어 어휘를 4종으로 결론낸 적이 있다(사건 2).
그리고 중복 제거를 하면 `valid_from`이 사라진다(미수 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from defusedxml.ElementTree import fromstring

from regchange.ingest.integrity import (
    IntegrityFailure,
    RecordKey,
    build_keys,
    check_counts,
    check_integrity,
    check_keys,
    find_revision_groups,
)
from regchange.ingest.masking import Masker
from regchange.ingest.response import (
    EFLAW_SEARCH,
    JO_HISTORY_BY_ARTICLE,
    JO_HISTORY_BY_DATE,
    LAW_DOCUMENT,
    LS_HISTORY,
    ClassifiedOk,
    TargetSpec,
    classify,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
CACHE = Path(__file__).resolve().parents[2] / "data" / "frequency-cache"


def ok(name: str, spec: TargetSpec) -> ClassifiedOk:
    result = classify((FIXTURES / name).read_bytes(), spec, masker=Masker("test-oc"))
    assert isinstance(result, ClassifiedOk), getattr(result, "detail", "")
    return result


# ---------------------------------------------------------------------------
# 1. 잘린 픽스처 3종 — 전부 실패로 처리되어야 한다
# ---------------------------------------------------------------------------

TRUNCATED = [
    pytest.param("jochg_009244_jo000200.xml", JO_HISTORY_BY_ARTICLE, 28, 20, id="조문별_28중20"),
    pytest.param("lschg_regdt20240719.xml", LS_HISTORY, 71, 5, id="lsHstInf_71중5"),
    pytest.param("search_eflaw_teukgeum.xml", EFLAW_SEARCH, 81, 10, id="eflaw검색_81중10"),
]


@pytest.mark.parametrize(("name", "spec", "total", "received"), TRUNCATED)
def test_truncated_fixtures_fail_integrity(
    name: str, spec: TargetSpec, total: int, received: int
) -> None:
    classified = ok(name, spec)
    report = check_integrity(spec, classified.total_count, classified.items, classified.articles)
    assert not report.ok
    assert IntegrityFailure.SHORT_COUNT in report.failures
    assert f"totalCnt={total}" in report.detail
    assert f"{received}건만" in report.detail


@pytest.mark.parametrize(("name", "spec", "total", "received"), TRUNCATED)
def test_truncated_fixtures_are_still_classified_ok(
    name: str, spec: TargetSpec, total: int, received: int
) -> None:
    """분류는 통과한다 — 잘림은 형태가 아니라 건수의 문제다.

    이 구분이 중요하다. 형태 분류만 있으면 잘린 응답이 정상으로 보인다.
    """
    classified = ok(name, spec)
    assert classified.total_count == total
    assert len(classified.items) == received


def test_complete_fixtures_pass_integrity() -> None:
    for name, spec in [
        ("jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE),
        ("dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE),
        ("dayjochg_regdt20210105.xml", JO_HISTORY_BY_DATE),
    ]:
        classified = ok(name, spec)
        report = check_integrity(
            spec, classified.total_count, classified.items, classified.articles
        )
        assert report.ok, report.detail


# ---------------------------------------------------------------------------
# 2. 완주 검사 대상이 계열마다 다르다
# ---------------------------------------------------------------------------


def test_history_counts_laws_not_articles() -> None:
    """조문 기준으로 검사하면 항상 실패한다 — 83 vs 286."""
    classified = ok("dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE)
    report = check_integrity(
        JO_HISTORY_BY_DATE, classified.total_count, classified.items, classified.articles
    )
    assert report.ok
    assert report.total_count == 83
    assert report.item_count == 83  # 대조 대상
    assert report.article_count == 286  # 대조 대상이 아니다
    assert "법령 수" in report.detail


def test_document_family_is_exempt_from_count_check() -> None:
    classified = ok("law_009244_mst252787.xml", LAW_DOCUMENT)
    report = check_integrity(
        LAW_DOCUMENT, classified.total_count, classified.items, classified.articles
    )
    assert report.ok
    assert report.total_count is None
    assert report.checked_keys == 0  # 검사하지 않았다는 사실이 기록된다
    assert "대상이 아니다" in report.detail


def test_missing_total_count_in_list_family_is_failure() -> None:
    """이력 계열에서 totalCnt를 확인할 수 없으면 실패다. 통과시키면 잘림이 정상으로 보인다."""
    failures, detail = check_counts(JO_HISTORY_BY_DATE, None, [])
    assert IntegrityFailure.SHORT_COUNT in failures
    assert "확인할 수 없는 것을 통과시키면" in detail


def test_over_count_is_also_failure() -> None:
    """많이 받은 것도 실패다. 누락만 검사하면 과잉을 놓친다."""
    items = fromstring(b"<r><law/><law/><law/></r>").findall("law")
    failures, detail = check_counts(JO_HISTORY_BY_DATE, 2, items)
    assert IntegrityFailure.OVER_COUNT in failures
    assert "부풀려진다" in detail


def test_zero_total_with_zero_items_is_complete() -> None:
    """0건은 완주다. 0건의 정당성은 이 모듈이 판단하지 않는다."""
    failures, _ = check_counts(JO_HISTORY_BY_DATE, 0, [])
    assert failures == ()


# ---------------------------------------------------------------------------
# 3. 식별키 — 계열별로 다른 시점 필드를 쓴다
# ---------------------------------------------------------------------------


def test_by_article_keys_have_no_collision_and_use_change_date() -> None:
    """조문별 28행이 조문변경일로 갈려 충돌 0건이다 (edge-case #18)."""
    classified = ok("jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE)
    keys = build_keys(JO_HISTORY_BY_ARTICLE, classified.items)
    assert len(keys) == 28
    assert len(set(keys)) == 28
    assert {key.source_field for key in keys} == {"조문변경일"}
    failures, detail, collisions = check_keys(keys)
    assert failures == ()
    assert collisions == ()
    assert "충돌 없음" in detail


def test_dropping_version_time_would_collapse_three_rows() -> None:
    """시점 필드를 빼면 3건이 사라진다. 그 3건이 valid_from이다.

    이 테스트는 "MST만으로 중복 제거했다면 무엇을 잃었는가"를 수치로 고정한다.
    """
    classified = ok("jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE)
    keys = build_keys(JO_HISTORY_BY_ARTICLE, classified.items)
    without_time = {(key.version_id, key.article_no) for key in keys}
    assert len(keys) - len(without_time) == 3


def test_by_date_keys_use_effective_date() -> None:
    classified = ok("dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE)
    keys = build_keys(JO_HISTORY_BY_DATE, classified.items)
    assert len(keys) == 286
    assert len(set(keys)) == 286
    assert {key.source_field for key in keys} == {"조문시행일"}


def test_key_collision_is_reported_with_full_fields() -> None:
    """충돌 시 키 전체를 남긴다 — 어느 필드를 키에 추가할지 판단해야 하므로."""
    duplicated = RecordKey("270399", "001103", "20250401", "조문시행일")
    failures, detail, collisions = check_keys([duplicated, duplicated])
    assert IntegrityFailure.KEY_COLLISION in failures
    assert collisions == ((duplicated, 2),)
    assert "키를 다시 검토할 신호" in detail


def test_empty_identity_fields_are_reported_separately() -> None:
    """빈 키끼리는 서로 같아 보인다 — 사건 1(dict 붕괴)과 같은 기전이다."""
    failures, detail, _ = check_keys([RecordKey("", "", "", "조문시행일")])
    assert IntegrityFailure.MISSING_IDENTITY in failures
    assert "한 건으로 집계된다" in detail


def test_ls_history_has_no_article_keys() -> None:
    """lsHstInf는 조문 수준 정보가 없다. 키 0건이 정상이며 건수 검사만 유효하다."""
    classified = ok("lschg_regdt20240719.xml", LS_HISTORY)
    assert build_keys(LS_HISTORY, classified.items) == []
    assert LS_HISTORY.article_path is None


# ---------------------------------------------------------------------------
# 4. 연혁을 제거하지 않는다 (edge-case #18)
# ---------------------------------------------------------------------------


def test_revision_group_is_detected_but_not_a_failure() -> None:
    """법령ID 006612가 MST 2종으로 나타난다. 연혁이며 실패가 아니다."""
    classified = ok("lschg_regdt20240719.xml", LS_HISTORY)
    groups = find_revision_groups(LS_HISTORY, classified.items)
    assert ("006612", 2) in groups

    report = check_integrity(
        LS_HISTORY, classified.total_count, classified.items, classified.articles
    )
    # 잘림 때문에 실패하지만, 연혁 자체는 실패 사유가 아니다.
    assert IntegrityFailure.KEY_COLLISION not in report.failures
    assert ("006612", 2) in report.revision_groups


def test_both_revisions_survive_no_deduplication_happens() -> None:
    """MST 264383과 230047이 **둘 다** 살아남는다. 중복 제거를 하지 않는다."""
    classified = ok("lschg_regdt20240719.xml", LS_HISTORY)
    rows = [(item.findtext("법령ID"), item.findtext("법령일련번호")) for item in classified.items]
    assert ("006612", "264383") in rows
    assert ("006612", "230047") in rows
    assert len(classified.items) == 5  # 5건 그대로. 하나도 줄지 않았다


def test_by_article_has_no_law_id_so_revision_groups_are_empty() -> None:
    """조문별 응답은 법령ID로 조회하므로 항목에 법령ID가 없다. 셀 의미가 없다."""
    classified = ok("jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE)
    assert JO_HISTORY_BY_ARTICLE.law_id_path is None
    assert find_revision_groups(JO_HISTORY_BY_ARTICLE, classified.items) == ()


# ---------------------------------------------------------------------------
# 5. 전수 검증 — 365일 캐시로 식별키를 확인한다
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CACHE.is_dir(), reason="12개월 캐시는 .gitignore 대상이다")
def test_identity_key_holds_across_the_full_year_cache() -> None:
    """캐시 365일 전수에서 식별키 충돌이 0건인지 확인한다.

    edge-case #18의 근거가 된 측정을 테스트로 고정한다. 캐시는 커밋되지 않으므로
    있는 환경에서만 돈다 — 없다고 통과하는 것이 아니라 skip 된다.
    """
    files = sorted(CACHE.glob("*.xml"))
    assert files, "캐시 디렉터리가 비어 있다"

    total_rows = 0
    for path in files:
        classified = classify(path.read_bytes(), JO_HISTORY_BY_DATE, masker=Masker("test-oc"))
        assert isinstance(classified, ClassifiedOk), path.name
        report = check_integrity(
            JO_HISTORY_BY_DATE, classified.total_count, classified.items, classified.articles
        )
        assert report.ok, f"{path.name}: {report.detail}"
        total_rows += report.checked_keys

    assert total_rows == 35681, f"조문 행 수가 실측(35,681)과 다르다: {total_rows}"
