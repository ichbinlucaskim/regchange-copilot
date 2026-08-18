"""변경 유형 판정 — 특히 EDITORIAL 과 조문 개수 보존.

이 테스트가 존재하는 이유:
(1) EDITORIAL 은 R-14(가짜 알림 폭주 → 알림 무시)의 유일한 방어선이다. 마커 날짜만
    바뀐 조문을 MODIFIED 로 보고하면 담당자는 수백 건의 가짜 알림을 받고 그 뒤로
    알림을 보지 않는다. 그 실패는 예외 없이 "정상 동작"으로 나타난다.
(2) 조문 개수 보존이 깨진 사고가 실제로 있었다 — dict 붕괴로 `0013001` 이 소실돼
    "벌칙 1:2"로 잘못 세었고 그 숫자가 ADR-003 의 근거로 쓰였다(사건 1).
    이 테스트는 그 사고가 재발하지 않는지 검사한다.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from regchange.diff import (
    EDITORIAL_PRIORITY_RANK,
    ArticleSnapshot,
    ChangeType,
    DiffError,
    classify,
    diff_articles,
    index_by_ref,
    priority_rank_for,
)


def snap(
    no: int,
    branch: int = 0,
    *,
    body: str = "본문",
    markers: tuple[str, ...] = (),
    title: str | None = None,
) -> ArticleSnapshot:
    """해시를 본문에서 결정론적으로 만든 스냅샷. 같은 본문이면 같은 해시다."""
    import hashlib

    return ArticleSnapshot(
        article_id=uuid4(),
        article_no=no,
        branch_no=branch,
        title=title,
        body_norm=body,
        body_norm_sha256=hashlib.sha256(body.encode()).hexdigest(),
        marker_signature=markers,
    )


def test_added_and_deleted_are_detected_by_coordinate() -> None:
    """to 에만 있으면 ADDED, from 에만 있으면 DELETED."""
    changes, counts = diff_articles([snap(1), snap(2)], [snap(1), snap(3)])
    by_type = {c.change_type: c.ref for c in changes}

    assert by_type[ChangeType.DELETED] == (2, 0)
    assert by_type[ChangeType.ADDED] == (3, 0)
    assert counts.added == 1
    assert counts.deleted == 1


def test_modified_when_body_hash_differs() -> None:
    """조립본 해시가 다르면 MODIFIED 다."""
    assert classify(snap(1, body="가"), snap(1, body="나")) is ChangeType.MODIFIED


def test_editorial_when_only_markers_differ() -> None:
    """본문이 같고 마커만 다르면 EDITORIAL 이다 — R-14 의 방어선."""
    before = snap(1, body="같은 본문", markers=("개정|2019-01-15",))
    after = snap(1, body="같은 본문", markers=("개정|2019-01-15,2020-03-24",))

    assert classify(before, after) is ChangeType.EDITORIAL


def test_unchanged_when_body_and_markers_match() -> None:
    """둘 다 같으면 UNCHANGED 이며 변경 행을 만들지 않는다."""
    changes, counts = diff_articles(
        [snap(1, markers=("개정|2020-03-24",))], [snap(1, markers=("개정|2020-03-24",))]
    )
    assert changes == ()
    assert counts.unchanged == 1


def test_body_change_wins_over_marker_change() -> None:
    """본문도 바뀌고 마커도 바뀌면 MODIFIED 다.

    EDITORIAL 로 분류하면 실질 변경이 최하위로 강등되어, 담당자가 봐야 할 것을
    맨 아래에서 보게 된다 — R-14 를 막으려다 반대 방향의 실패를 만드는 셈이다.
    """
    before = snap(1, body="가", markers=("개정|2019-01-15",))
    after = snap(1, body="나", markers=("개정|2020-03-24",))

    assert classify(before, after) is ChangeType.MODIFIED


def test_editorial_is_demoted_not_dropped() -> None:
    """EDITORIAL 은 행으로 남고 우선순위만 최하위가 된다 (완료 조건 2).

    감사에서 "이 문구정비 개정은 왜 검토 안 했나"를 물으면 인지했고 등급을 매겼다고
    답할 수 있어야 한다. 행이 없으면 그 답이 불가능하다.
    """
    changes, counts = diff_articles(
        [snap(1, body="같음", markers=("개정|2019-01-15",))],
        [snap(1, body="같음", markers=("개정|2020-03-24",))],
    )

    assert len(changes) == 1, "EDITORIAL 도 행으로 남는다"
    assert changes[0].change_type is ChangeType.EDITORIAL
    assert changes[0].priority_rank == EDITORIAL_PRIORITY_RANK
    assert counts.editorial == 1


def test_only_editorial_is_demoted() -> None:
    """다른 유형은 기본 우선순위를 유지한다."""
    assert priority_rank_for(ChangeType.EDITORIAL) == EDITORIAL_PRIORITY_RANK
    for other in (ChangeType.ADDED, ChangeType.DELETED, ChangeType.MODIFIED):
        assert priority_rank_for(other) < EDITORIAL_PRIORITY_RANK


def test_branch_articles_are_distinct_coordinates() -> None:
    """제5조와 제5조의2 는 다른 조문이다. 문자열이 아니라 구조로 짝짓는다 (ADR-001)."""
    changes, counts = diff_articles([snap(5)], [snap(5), snap(5, 2)])

    assert counts.added == 1
    assert changes[0].ref == (5, 2)


def test_duplicate_coordinate_fails_instead_of_being_overwritten() -> None:
    """좌표가 중복되면 조용히 덮어쓰지 않고 실패한다 (사건 1).

    dict 로 색인하되 덮어쓰는 순간을 잡는다. 덮어쓰면 조문 하나가 사라지고,
    그 사라짐은 합계가 맞아 보이는 형태로 나타난다.
    """
    with pytest.raises(DiffError, match="중복"):
        index_by_ref([snap(13), snap(13)])


def test_count_preservation_holds_both_directions() -> None:
    """from + ADDED == to + DELETED 가 양방향으로 성립한다."""
    from_side = [snap(1), snap(2), snap(3)]
    to_side = [snap(1), snap(3, body="바뀜"), snap(4)]
    _, counts = diff_articles(from_side, to_side)

    assert counts.from_article_count + counts.added == counts.to_article_count + counts.deleted
    counts.verify(context="테스트")


def test_verify_raises_not_asserts() -> None:
    """`assert` 가 아니라 예외를 던진다. `python -O` 에서도 검사가 살아 있어야 한다."""
    from regchange.diff import DiffCounts

    with pytest.raises(DiffError, match="조문 개수 보존"):
        DiffCounts(from_article_count=5, to_article_count=5).verify(context="테스트")


def test_changes_are_ordered_deterministically() -> None:
    """같은 입력에 항상 같은 순서로 나온다 — 감사 재현에서 두 실행을 대조할 수 있어야 한다."""
    from_side = [snap(3), snap(1), snap(2)]
    to_side = [snap(2, body="바뀜"), snap(9), snap(1, body="바뀜")]

    first = [c.ref for c in diff_articles(from_side, to_side)[0]]
    second = [c.ref for c in diff_articles(list(reversed(from_side)), list(reversed(to_side)))[0]]

    assert first == second == sorted(first)
