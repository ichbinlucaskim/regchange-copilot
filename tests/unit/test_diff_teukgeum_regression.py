"""특금법 2011↔2020 회귀 테스트 — 조문 개수 보존이 이 파일의 핵심이다.

이 테스트가 존재하는 이유:
    초기 집계에서 dict 붕괴로 2011판 `0013001` 이 소실돼 "벌칙 1:2"로 잘못 셌고,
    **그 틀린 숫자가 ADR-003 의 1차 근거로 쓰였다.** 실제로는 2:2 다
    (`docs/incidents/silent-undercounting.md` 사건 1). 이 파일은 그 사고가
    재발하지 않는지 검사한다.

    합계가 맞는 것으로 전수 검사를 증명한다 — 조문 하나가 조용히 사라지면
    `from + ADDED == to + DELETED` 가 깨진다.

DB 를 쓰지 않는다. 판정이 순수 함수이므로 픽스처만으로 고정할 수 있다 (원칙 1).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from regchange.diff import (
    EvidenceKind,
    MoveWindow,
    diff_versions,
    snapshot_from_unit,
)
from regchange.parse.law_xml import parse_law_document
from regchange.parse.models import LawDocument

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
FROM_FIXTURE = FIXTURES / "law_009244_mst113262_v20110519.xml"
TO_FIXTURE = FIXTURES / "law_009244_mst215971_v20200324.xml"
LATER_FIXTURE = FIXTURES / "law_009244_mst252787.xml"

EXPECTED_EXPLICIT_EDGES = 15
"""명시 표기 기준 이동 건수 (완료 조건 9). 가지번호 조문 3건을 포함한다."""

EXPECTED_UNRESOLVED = 3
"""그중 후보를 만들 수 없는 건수 — 출발 조문이 2011판에 존재하지 않는다."""


def window_between(source: LawDocument, target: LawDocument) -> MoveWindow:
    """두 문서의 공포일자로 날짜 창을 만든다. 공포일자가 없으면 창을 정할 수 없다."""
    assert source.promulgation_date is not None
    assert target.promulgation_date is not None
    return MoveWindow(after=source.promulgation_date, through=target.promulgation_date)


@pytest.fixture(scope="module")
def diff_result():  # type: ignore[no-untyped-def]
    """2011판 → 2020판 비교 결과."""
    source = parse_law_document(FROM_FIXTURE)
    target = parse_law_document(TO_FIXTURE)
    return diff_versions(
        [snapshot_from_unit(u) for u in source.articles],
        [snapshot_from_unit(u) for u in target.articles],
        window=window_between(source, target),
    )


def test_article_count_is_preserved(diff_result) -> None:  # type: ignore[no-untyped-def]
    """`len(from) + ADDED == len(to) + DELETED` — 이 파일에서 가장 중요한 단언이다."""
    counts = diff_result.counts

    assert counts.from_article_count == 19
    assert counts.to_article_count == 27
    assert counts.from_article_count + counts.added == counts.to_article_count + counts.deleted
    counts.verify(context="특금법 2011↔2020")


def test_no_article_is_silently_dropped(diff_result) -> None:  # type: ignore[no-untyped-def]
    """처분을 받지 않은 조문이 없다 — 사건 1이 검사되는 지점이다."""
    counts = diff_result.counts
    dispositioned = counts.deleted + counts.modified + counts.editorial + counts.unchanged

    assert dispositioned == counts.from_article_count
    assert counts.added + counts.modified + counts.editorial + counts.unchanged == (
        counts.to_article_count
    )


def test_penalty_articles_are_two_to_two_not_one_to_two() -> None:
    """벌칙 조문이 양쪽 버전 모두 2건이다 — ADR-003 의 1차 근거가 틀렸던 지점.

    2011판 `0013001`(제13조)이 dict 붕괴로 소실돼 1건으로 보였고, 그래서 "1:2 로
    갈라진다"가 근거가 됐다. 실제로는 2:2 다.
    """
    source = parse_law_document(FROM_FIXTURE)
    target = parse_law_document(TO_FIXTURE)

    from_penalty = [u.ref.render() for u in source.articles if u.title == "벌칙"]
    to_penalty = [u.ref.render() for u in target.articles if u.title == "벌칙"]

    assert len(from_penalty) == 2, f"2011판 벌칙 조문이 2건이어야 한다: {from_penalty}"
    assert len(to_penalty) == 2, f"2020판 벌칙 조문이 2건이어야 한다: {to_penalty}"


def test_explicit_moves_are_fifteen(diff_result) -> None:  # type: ignore[no-untyped-def]
    """명시 표기 기준 이동이 15건이다 (완료 조건 9).

    정규식이 조사 하나를 놓쳐 128건이 75건으로 줄었던 사고(사건 3)가 이 숫자로
    드러난다. 15가 12나 13이 되면 파서가 표기를 놓친 것이다.
    """
    assert diff_result.explicit_edge_count == EXPECTED_EXPLICIT_EDGES
    assert diff_result.moves_in_window == 27, "MOVED_FROM 15 + PREVIOUS_MOVED_TO 12"
    assert diff_result.moves_out_of_window == 0


def test_branch_numbered_moves_are_included(diff_result) -> None:  # type: ignore[no-untyped-def]
    """가지번호 조문 3건이 명시 표기에 들어 있다.

    제목 매칭은 이 3건을 놓쳤다 (ADR-003 근거 c). 명시 표기를 쓰는 이유다.
    """
    unresolved = set(diff_result.unresolved_explicit_edges)

    assert unresolved == {((7, 2), (10, 2)), ((9, 2), (12, 2)), ((11, 2), (15, 2))}
    assert len(unresolved) == EXPECTED_UNRESOLVED


def test_unresolved_edges_explain_the_gap(diff_result) -> None:  # type: ignore[no-untyped-def]
    """명시 15건과 EXPLICIT 후보 12건의 차이가 기록으로 설명된다.

    설명이 없으면 다음 사람이 파서가 3건을 놓쳤다고 의심한다 — 실제로 그런 사고가
    있었으므로(사건 3) 그 의심은 합리적이다. 그래서 차이를 값으로 남긴다.
    """
    explicit = [c for c in diff_result.candidates if c.evidence_kind is EvidenceKind.EXPLICIT]

    assert len(explicit) + len(diff_result.unresolved_explicit_edges) == (
        diff_result.explicit_edge_count
    )
    assert len(explicit) == 12


def test_explicit_candidates_are_one_to_one(diff_result) -> None:  # type: ignore[no-untyped-def]
    """명시 근거끼리는 1:1 이다. 유사도 후보의 소음이 이 판정을 흐리지 않는다."""
    explicit = [c for c in diff_result.candidates if c.evidence_kind is EvidenceKind.EXPLICIT]

    assert {c.cardinality.value for c in explicit} == {"1:1"}


def test_all_candidates_carry_three_signals(diff_result) -> None:  # type: ignore[no-untyped-def]
    """모든 후보에 세 신호가 함께 담긴다 (ADR-003).

    세 신호가 엇갈리는 것이 파서 버그의 신호이므로, 검토자가 그것을 볼 수 있어야 한다.
    """
    for candidate in diff_result.candidates:
        assert set(candidate.evidence) >= {
            "explicit",
            "explicit_raw",
            "title_available",
            "title_match",
            "similarity",
        }


def test_stale_moves_are_filtered_when_comparing_later_versions() -> None:
    """같은 표기를 지닌 2023 공포판을 to 로 쓰면 창 밖으로 걸러진다.

    `조문참고자료` 는 이동 이력을 누적하므로, 창이 없으면 2020년 이동이 2020→2023
    diff 에서 또 후보가 된다. 검토자는 같은 이동을 두 번 본다.
    """
    source = parse_law_document(TO_FIXTURE)
    target = parse_law_document(LATER_FIXTURE)
    result = diff_versions(
        [snapshot_from_unit(u) for u in source.articles],
        [snapshot_from_unit(u) for u in target.articles],
        window=window_between(source, target),
    )

    assert result.moves_in_window == 0
    assert result.moves_out_of_window == 27
    assert result.out_of_window_dates == ("2020-03-24",)
    assert result.candidates == ()


def test_move_notes_are_historical_not_version_local() -> None:
    """이동 표기의 날짜가 문서 시행일과 다르다 — 이력 누적임을 고정한다.

    이 성질을 모르고 표기를 그대로 쓰면 2008년 이동이 오늘의 후보가 된다.
    실측: 픽스처 전체 128건 중 문서 시행 연도와 같은 해가 0건이다.
    """
    document = parse_law_document(TO_FIXTURE)
    move_dates = {move.dates[0] for unit in document.articles for move in unit.moves if move.dates}

    assert move_dates == {dt.date(2020, 3, 24)}
    assert document.document_effective_date == dt.date(2021, 3, 25)
    assert all(d != document.document_effective_date for d in move_dates)


def test_assembled_body_finds_changes_that_article_content_alone_misses() -> None:
    """조립본 기준이 `조문내용` 기준보다 변경을 더 잡는다 — 놓친 변경이 0건이 된다.

    이 테스트가 고정하는 것: 작업 3이 저장한 `text_norm_sha256`(조문내용만의 해시)로
    비교하면 항이 있는 조문의 본문 변경을 통째로 놓친다는 사실. 실측으로 놓친 변경이
    5건이었고 전부 항/호/목에 있었다 (edge-case #5).

    정답지(`조문변경여부`)는 "이 공포에서 변경"을 뜻하므로 이 두 버전 쌍에는 적용되지
    않는다. 그래서 정확도 수치가 아니라 **놓친 변경(FN)이 0인지**만 단언한다 —
    적용 범위 밖의 정답지로 목표 달성을 주장하지 않는다.
    """
    source = parse_law_document(FROM_FIXTURE)
    target = parse_law_document(TO_FIXTURE)

    from_articles = {u.ref.jo: u for u in source.articles}
    to_articles = {u.ref.jo: u for u in target.articles}
    flagged = {key for key, unit in to_articles.items() if unit.changed}

    def changed_by(key_fn) -> set[str]:  # type: ignore[no-untyped-def]
        added = set(to_articles) - set(from_articles)
        modified = {
            key
            for key in set(to_articles) & set(from_articles)
            if key_fn(from_articles[key]) != key_fn(to_articles[key])
        }
        return added | modified

    from regchange.parse.assemble import assemble_body

    content_only = changed_by(lambda u: u.content.sha256)
    assembled = changed_by(lambda u: assemble_body(u).sha256)

    assert len(flagged - content_only) == 5, "조문내용만 비교하면 5건을 놓친다"
    assert flagged - assembled == set(), "전문을 조립하면 놓치는 변경이 없다"
    assert content_only < assembled, "조립본 기준이 조문내용 기준을 포함한다"


def test_false_positives_are_explained_by_the_ground_truth_semantics() -> None:
    """정답지가 '변경 아님'이라 한 2건은 2020 공포에서 바뀐 것이 아니다.

    `조문변경여부='Y'` 는 "직전 버전 대비"가 아니라 "이 공포에서 변경"이다.
    제5조는 2012·2013·2019 개정이고 제5조의3은 2013 신설이므로, 2020-03-24 공포
    기준으로는 `N` 이 맞다. **우리 diff 가 틀린 것이 아니라 정답지가 다른 질문에
    답한다.**
    """
    target = parse_law_document(TO_FIXTURE)
    by_ref = {u.ref.jo: u for u in target.articles}

    article_5 = by_ref["000500"]
    article_5_3 = by_ref["000503"]

    assert article_5.changed is False
    assert article_5_3.changed is False
    assert target.promulgation_date is not None
    assert all(
        target.promulgation_date not in marker.dates for marker in article_5.content.markers
    ), "제5조에 2020-03-24 마커가 없다"
    assert article_5_3.reference_raw is not None
    assert "본조신설 2013" in article_5_3.reference_raw
