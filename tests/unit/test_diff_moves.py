"""이동 후보 — 세 신호, 날짜 창, cardinality, 그리고 자동 확정 금지.

이 테스트가 존재하는 이유:
(1) `조문참고자료` 는 그 조문의 이동 이력 **전체**를 누적한다. 실측에서 표기 128건 중
    문서 시행 연도와 같은 해가 0건이고 소득세법 한 문서가 4개 시점을 담는다.
    날짜 창 없이 쓰면 2008년 이동이 오늘의 후보로 검토 큐에 올라간다.
(2) 1:N 을 억지로 1:1 로 줄이면 한쪽이 조용히 사라진다 (ADR-003).
(3) 명시 표기가 있어도 확정하지 않는다. 파서 버그가 곧 잘못된 이동이 되기 때문이며,
    실제로 정규식이 조사 하나를 놓쳐 53건을 누락한 적이 있다 (사건 3).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from uuid import uuid4

from regchange.diff import (
    EXPLICIT_SCORE,
    ArticleSnapshot,
    Cardinality,
    EvidenceKind,
    MoveWindow,
    build_move_candidates,
)
from regchange.parse.models import ArticleRef, MoveKind, MoveReference

WINDOW = MoveWindow(after=dt.date(2011, 5, 19), through=dt.date(2020, 3, 24))


def snap(
    no: int,
    branch: int = 0,
    *,
    body: str = "본문",
    title: str | None = None,
    moves: tuple[MoveReference, ...] = (),
) -> ArticleSnapshot:
    return ArticleSnapshot(
        article_id=uuid4(),
        article_no=no,
        branch_no=branch,
        title=title,
        body_norm=body,
        body_norm_sha256=hashlib.sha256(body.encode()).hexdigest(),
        moves=moves,
    )


def moved_from(no: int, when: dt.date, *, branch: int = 0) -> MoveReference:
    return MoveReference(
        kind=MoveKind.MOVED_FROM,
        source=ArticleRef(article_no=no, branch_no=branch),
        dates=(when,),
        raw=f"[제{no}조에서 이동 <{when}>]",
    )


def test_explicit_reference_becomes_a_candidate_with_raw_preserved() -> None:
    """명시 표기가 후보가 되고 파싱 원문이 evidence 에 남는다 (ADR-003)."""
    to_article = snap(9, moves=(moved_from(6, dt.date(2020, 3, 24)),))
    candidates, metrics = build_move_candidates([snap(6)], [to_article], window=WINDOW)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.from_ref == (6, 0)
    assert candidate.to_ref == (9, 0)
    assert candidate.evidence_kind is EvidenceKind.EXPLICIT
    assert candidate.score == EXPLICIT_SCORE
    assert candidate.evidence["explicit_raw"] == "[제6조에서 이동 <2020-03-24>]"
    assert metrics.moves_in_window == 1


def test_move_outside_the_window_is_filtered_and_counted() -> None:
    """창 밖 표기는 **명시 근거가 되지 못하고**, 걸러진 건수가 남는다.

    조용히 버리면 "몇 건을 왜 제외했나"에 답할 수 없고 필터가 틀려도 발견되지 않는다.

    주의: 제6조는 DELETED, 제9조는 ADDED 이므로 그 쌍은 창과 무관하게 fallback
    후보로 남는다. 창이 막는 것은 **후보의 존재가 아니라 EXPLICIT 근거의 부여**다.
    2008년 표기가 오늘의 "법제처 명시"로 제시되는 것을 막는 것이 목적이다.
    """
    stale = snap(9, moves=(moved_from(6, dt.date(2008, 11, 11)),))
    candidates, metrics = build_move_candidates([snap(6)], [stale], window=WINDOW)

    assert all(c.evidence_kind is not EvidenceKind.EXPLICIT for c in candidates)
    assert all(c.evidence["explicit"] is False for c in candidates)
    assert metrics.explicit_edge_count == 0
    assert metrics.moves_in_window == 0
    assert metrics.moves_out_of_window == 1
    assert metrics.out_of_window_dates == ("2008-11-11",)


def test_window_boundaries_are_from_exclusive_and_to_inclusive() -> None:
    """`from` 공포일은 제외, `to` 공포일은 포함이다."""
    assert not WINDOW.contains(dt.date(2011, 5, 19))
    assert WINDOW.contains(dt.date(2011, 5, 20))
    assert WINDOW.contains(dt.date(2020, 3, 24))
    assert not WINDOW.contains(dt.date(2020, 3, 25))
    assert not WINDOW.contains(None), "날짜 없는 표기는 창 밖으로 다룬다"


def test_title_match_is_used_when_no_explicit_reference() -> None:
    """명시 표기가 없으면 제목 일치가 fallback 이 된다."""
    candidates, _ = build_move_candidates(
        [snap(1, title="벌칙", body="가")],
        [snap(2, title="벌칙", body="나")],
        window=WINDOW,
    )
    assert [c.evidence_kind for c in candidates] == [EvidenceKind.TITLE]


def test_similarity_is_used_when_no_title_signal() -> None:
    """제목이 없으면 유사도가 신호가 된다. 제목 부재를 evidence 에 남긴다."""
    candidates, _ = build_move_candidates(
        [snap(1, body="같은 본문입니다")],
        [snap(2, body="같은 본문입니다")],
        window=WINDOW,
    )
    assert candidates[0].evidence_kind is EvidenceKind.SIMILARITY
    assert candidates[0].evidence["title_available"] is False
    assert candidates[0].score == 1.0


def test_one_to_many_is_not_collapsed_into_one_to_one() -> None:
    """1:N 을 억지로 1:1 로 줄이지 않는다 (완료 조건 5, ADR-003)."""
    source = snap(13, title="벌칙")
    targets = [
        snap(16, title="벌칙", moves=(moved_from(13, dt.date(2020, 3, 24)),)),
        snap(17, title="벌칙", moves=(moved_from(13, dt.date(2020, 3, 24)),)),
    ]
    candidates, _ = build_move_candidates([source], targets, window=WINDOW)

    explicit = [c for c in candidates if c.evidence_kind is EvidenceKind.EXPLICIT]
    assert len(explicit) == 2, "두 후보가 모두 남는다"
    assert {c.cardinality for c in explicit} == {Cardinality.ONE_TO_MANY}


def test_many_to_one_is_recorded() -> None:
    """N:1 도 그대로 기록된다."""
    sources = [snap(13), snap(14)]
    target = snap(
        16, moves=(moved_from(13, dt.date(2020, 3, 24)), moved_from(14, dt.date(2020, 3, 24)))
    )
    candidates, _ = build_move_candidates(sources, [target], window=WINDOW)

    explicit = [c for c in candidates if c.evidence_kind is EvidenceKind.EXPLICIT]
    assert len(explicit) == 2
    assert {c.cardinality for c in explicit} == {Cardinality.MANY_TO_ONE}


def test_explicit_edge_without_a_source_article_is_recorded_not_dropped() -> None:
    """출발 조문이 from 버전에 없으면 후보를 만들 수 없다 — 그 사실을 남긴다.

    실측: 특금법 2011↔2020 의 명시 표기 15건 중 3건이 여기 해당한다. 기록하지 않으면
    "명시 15건인데 후보 12건"의 차이를 설명할 수 없고, 다음 사람이 파서가 놓쳤다고
    의심하게 된다 — 실제로 그런 사고가 있었다 (사건 3).
    """
    to_article = snap(10, branch=2, moves=(moved_from(7, dt.date(2020, 3, 24), branch=2),))
    candidates, metrics = build_move_candidates([snap(1)], [to_article], window=WINDOW)

    assert all(c.evidence_kind is not EvidenceKind.EXPLICIT for c in candidates)
    assert metrics.explicit_edge_count == 1
    assert metrics.unresolved_explicit_edges == (((7, 2), (10, 2)),)


def test_empty_body_article_is_excluded_from_similarity_and_recorded() -> None:
    """본문이 빈 조문은 유사도에서 빠지고 좌표가 남는다. 조용히 0점 처리하지 않는다."""
    candidates, metrics = build_move_candidates(
        [snap(1, body="")], [snap(2, body="")], window=WINDOW
    )

    assert candidates == ()
    assert set(metrics.empty_body_refs) == {(1, 0), (2, 0)}


def test_previous_deleted_does_not_create_a_move_edge() -> None:
    """`종전 제N조는 삭제` 는 이동이 아니다."""
    deleted_note = MoveReference(
        kind=MoveKind.PREVIOUS_DELETED,
        source=ArticleRef(article_no=34, branch_no=2),
        dates=(dt.date(2020, 3, 24),),
        raw="[종전 제34조의2는 삭제 <2020.3.24>]",
    )
    candidates, metrics = build_move_candidates(
        [snap(34, 2)], [snap(1, moves=(deleted_note,))], window=WINDOW
    )

    assert all(c.evidence_kind is not EvidenceKind.EXPLICIT for c in candidates)
    assert metrics.explicit_edge_count == 0
