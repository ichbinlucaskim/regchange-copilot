"""검토 큐 질의와 승인 경로 — **기한 초과와 기한 미상을 구별하는지**가 핵심이다.

이 테스트가 존재하는 이유: 기한을 우리가 정하지 않기로 했고(개정 시행일이 기한이다),
그래서 "기한을 모르는 대기"라는 상태가 생겼다. 그것을 기한 초과로 세면 담당자를 재촉하게
되지만 **고쳐야 할 것은 수집 경로**다. 두 값이 합쳐지는 순간 그 구별이 사라진다.

그리고 승인 경로가 재승인을 막는지 확인한다 — 막지 못하면 발송이 두 번 일어난다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.ops.history import AlertKind, fetch_alerts
from regchange.review.models import DecisionRequest, ReasonCode, ReviewDecisionKind, ReviewState
from regchange.review.queue import (
    ReviewError,
    count_overdue,
    insert_assessment,
    list_pending,
    load_item,
    record_decision,
    summarize_reviews,
)
from regchange.store.dsn import DbRole

pytestmark = pytest.mark.requires_db

NOW = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.UTC)
AS_OF = dt.date(2026, 2, 1)

DRAFT: dict[str, Any] = {
    "status": "DRAFT",
    "impacts": [{"paragraph_id": "p1", "quote": "q", "claim": "c", "control_items": []}],
    "departments": [{"department": "정보보호부", "basis_paragraph_id": "p1"}],
    "risk_level": "HIGH",
    "required_evidence": ["신고 이력"],
}


async def _seed(
    conn: psycopg.AsyncConnection[Any],
    *,
    queued: bool = True,
    due_at: dt.datetime | None = None,
    status: str = "OK",
    created_at: dt.datetime = NOW,
) -> UUID:
    assessment_id = uuid4()
    await insert_assessment(
        conn,
        assessment_id=assessment_id,
        thread_id=str(assessment_id),
        law_name="정보통신망법",
        article_path="제48조의3",
        revision_kind="일부개정",
        change_type="MODIFIED",
        as_of=AS_OF,
        status=status,
        obligation_type="STRENGTHENED",
        risk_level="HIGH",
        confidence="MEDIUM",
        summary="요약",
        reason="사유",
        revisions=0,
        draft=DRAFT,
        grounding={"counts": {"SUPPORTED": 1}},
        discarded=[],
        queued=queued,
        due_at=due_at,
        created_at=created_at,
    )
    await conn.commit()
    return assessment_id


async def test_insufficient_evidence_is_recorded_but_not_queued(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """근거 부족 건은 **행은 남기고 큐에는 넣지 않는다.**

    행이 없으면 이관 비율(ADR-013 신호 4번)을 셀 수 없고, 큐에 넣으면 사람이 볼 것이
    없는 항목이 대기 목록을 채운다.
    """
    assessment_id = await _seed(owner_conn, queued=False, status="INSUFFICIENT_EVIDENCE")

    item = await load_item(owner_conn, assessment_id)
    assert item is not None
    assert item.review_state is ReviewState.NOT_QUEUED
    assert item.queued_at is None
    assert await list_pending(owner_conn) == ()


async def test_unknown_due_is_counted_separately_from_overdue(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """**기한 초과와 기한 미상은 다른 숫자다.** 조치가 다르면 지표도 달라야 한다."""
    await _seed(owner_conn, due_at=NOW - dt.timedelta(days=3))  # 기한 초과
    await _seed(owner_conn, due_at=NOW + dt.timedelta(days=30))  # 여유 있음
    await _seed(owner_conn, due_at=None)  # 시행일 미상

    counts = await count_overdue(owner_conn, now=NOW)

    assert counts.pending == 3
    assert counts.overdue == 1
    assert counts.unknown_due == 1


async def test_pending_list_orders_by_due_date_with_unknown_last(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """기한이 이른 것부터, 기한 미상은 뒤에 둔다. 앞에 두면 임박한 건이 밀린다."""
    late = await _seed(owner_conn, due_at=NOW + dt.timedelta(days=30))
    soon = await _seed(owner_conn, due_at=NOW + dt.timedelta(days=1))
    unknown = await _seed(owner_conn, due_at=None)

    order = [UUID(item.id) for item in await list_pending(owner_conn)]

    assert order == [soon, late, unknown]


async def test_alerts_separate_overdue_from_unknown_due(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """`ops alerts` 가 두 사실을 다른 알림으로 낸다."""
    await _seed(owner_conn, due_at=NOW - dt.timedelta(days=1))
    await _seed(owner_conn, due_at=None)

    kinds = {alert.kind for alert in await fetch_alerts(owner_conn, days=7, now=NOW)}

    assert AlertKind.REVIEW_OVERDUE in kinds
    assert AlertKind.REVIEW_DUE_UNKNOWN in kinds


async def test_accept_creates_exactly_one_outbox_row(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """수락은 승인 레코드 하나와 발송 대상 하나를 만든다. 그 이상도 이하도 아니다."""
    assessment_id = await _seed(owner_conn, due_at=NOW + dt.timedelta(days=10))
    review_conn = await role_connect(DbRole.REVIEW)

    decision_id, outbox_ids = await record_decision(
        review_conn,
        assessment_id=assessment_id,
        request=DecisionRequest(
            decision=ReviewDecisionKind.ACCEPT, decided_by="reviewer", reviewed_ms=5000
        ),
        decided_at=NOW,
    )
    await review_conn.commit()

    assert len(outbox_ids) == 1
    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT payload FROM action_outbox WHERE id = %s", (outbox_ids[0],))
        row = await cur.fetchone()
    assert row is not None
    payload = row["payload"]
    assert payload["affected_paragraph_ids"] == ["p1"]
    assert payload["departments"] == ["정보보호부"]
    assert "prompt" not in payload, "발송 대상에 프롬프트가 담기면 원칙 5 위반이다"
    assert str(decision_id)


async def test_reject_requires_reason_and_makes_no_outbox(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """반려는 사유와 함께 기록되고 발송 대상을 만들지 않는다."""
    assessment_id = await _seed(owner_conn, due_at=NOW + dt.timedelta(days=10))
    review_conn = await role_connect(DbRole.REVIEW)

    _decision_id, outbox_ids = await record_decision(
        review_conn,
        assessment_id=assessment_id,
        request=DecisionRequest(
            decision=ReviewDecisionKind.REJECT,
            decided_by="reviewer",
            reason_code=ReasonCode.INSUFFICIENT_BASIS,
            reason_note="인용이 주장을 뒷받침하지 않는다",
            reviewed_ms=120_000,
        ),
        decided_at=NOW,
    )
    await review_conn.commit()

    assert outbox_ids == ()
    summary = await summarize_reviews(owner_conn)
    assert summary["reject_rate"] == 1.0
    assert summary["reasons"][0]["reason_code"] == "INSUFFICIENT_BASIS"


async def test_second_decision_is_refused(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """이미 결정된 건에 두 번째 결정을 기록하지 않는다 — 발송이 두 번 일어난다."""
    assessment_id = await _seed(owner_conn, due_at=NOW + dt.timedelta(days=10))
    review_conn = await role_connect(DbRole.REVIEW)
    request = DecisionRequest(
        decision=ReviewDecisionKind.ACCEPT, decided_by="reviewer", reviewed_ms=100
    )

    await record_decision(review_conn, assessment_id=assessment_id, request=request)
    await review_conn.commit()

    with pytest.raises(ReviewError, match="이미"):
        await record_decision(review_conn, assessment_id=assessment_id, request=request)


async def test_decision_on_not_queued_item_is_refused(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """큐에 없는 건(이관됨)은 사람이 처리할 대상이 아니다. 사유가 구체적이어야 한다."""
    assessment_id = await _seed(owner_conn, queued=False, status="INSUFFICIENT_EVIDENCE")
    review_conn = await role_connect(DbRole.REVIEW)

    with pytest.raises(ReviewError, match="검토 큐에 없다"):
        await record_decision(
            review_conn,
            assessment_id=assessment_id,
            request=DecisionRequest(
                decision=ReviewDecisionKind.ACCEPT, decided_by="reviewer", reviewed_ms=1
            ),
        )
