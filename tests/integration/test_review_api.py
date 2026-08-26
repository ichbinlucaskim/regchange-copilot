"""검토 UI — **API 가 승인 레코드를 쓰지 않는다**는 것이 이 테스트의 핵심이다.

이 테스트가 존재하는 이유: API 가 `review_decision` 을 직접 INSERT 하면 "그래프를 거치지
않고 승인하는 경로"가 생긴다. 원칙 4 가 막으려는 바로 그것이며, 코드만 봐서는 나중에
누군가 편의로 추가할 수 있다. 재개가 아무 일도 하지 않았을 때 **409 를 돌려주는지**가
그 성질을 밖에서 확인하는 방법이다.

화면이 무엇을 보여주는지도 함께 고정한다. 결론만 보이면 승인은 통과 의식이 된다 (F-7).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from regchange.api.app import create_app
from regchange.review.models import DecisionRequest, ReviewDecisionKind
from regchange.review.queue import insert_assessment, record_decision
from regchange.store.dsn import DbRole, assert_disposable_database, role_dsn

pytestmark = pytest.mark.requires_db

NOW = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.UTC)
TEST_DATABASE = "regchange_test"

DRAFT: dict[str, Any] = {
    "status": "NEEDS_REVIEW",
    "obligation_type": "STRENGTHENED",
    "risk_level": "HIGH",
    "confidence": "MEDIUM",
    "summary": "신고 기한 정비 필요",
    "reason": "판단이 갈리는 지점이 있다",
    "impacts": [
        {
            "paragraph_id": "p1",
            "quote": "정보보호부장은 침해사고를 인지한 즉시",
            "claim": "신고 시점을 24시간 기준으로 고쳐야 한다",
            "obligation_index": 0,
            "control_items": ["신고 시점 문구 정비"],
        }
    ],
    "departments": [
        {
            "department": "경영지원부",
            "basis_paragraph_id": "p-other",
            "basis_quote": "예산은 경영지원부장이 편성한다",
            "derivation": "SUBJECT_IN_TEXT",
            "rationale": "예산 편성 주체가 협의 대상이다",
        }
    ],
    "required_evidence": ["신고 이력"],
}

GROUNDING: dict[str, Any] = {
    "counts": {"SUPPORTED": 1, "PARTIAL": 0, "UNSUPPORTED": 1},
    "unsupported": ["impact:1"],
    "judgments": [
        {"key": "impact:0", "level": "SUPPORTED", "reason": "문단이 주장을 담고 있다"},
        {"key": "impact:1", "level": "UNSUPPORTED", "reason": "소재만 겹친다"},
    ],
}


def _test_dsn() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(role_dsn(DbRole.REVIEW))
    return urlunsplit(parsed._replace(path=f"/{assert_disposable_database(TEST_DATABASE)}"))


async def _seed(conn: psycopg.AsyncConnection[Any]) -> UUID:
    assessment_id = uuid4()
    await insert_assessment(
        conn,
        assessment_id=assessment_id,
        thread_id=str(assessment_id),
        law_name="정보통신망법",
        article_path="제48조의3",
        revision_kind="일부개정",
        change_type="MODIFIED",
        as_of=dt.date(2026, 2, 1),
        status="NEEDS_REVIEW",
        obligation_type="STRENGTHENED",
        risk_level="HIGH",
        confidence="MEDIUM",
        summary="신고 기한 정비 필요",
        reason="판단이 갈리는 지점이 있다",
        revisions=1,
        draft=DRAFT,
        grounding=GROUNDING,
        discarded=[{"kind": "IMPACT", "reason": "QUOTE_NOT_FOUND", "label": "지어낸 인용"}],
        queued=True,
        due_at=NOW + dt.timedelta(days=20),
        created_at=NOW,
    )
    await conn.commit()
    return assessment_id


def _client(resume: Any) -> TestClient:
    return TestClient(create_app(resume=resume, dsn=_test_dsn()))


async def test_detail_screen_shows_what_makes_approval_hard(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """화면이 **경고·판정 이유·간접 도출·폐기 기록**을 모두 편다 (F-7).

    넷 중 하나라도 빠지면 검토자는 결론만 보고 승인하게 된다.
    """
    assessment_id = await _seed(owner_conn)

    async def resume(_thread: str, _decision: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("상세 화면은 그래프를 재개하지 않는다")

    with _client(resume) as client:
        body = client.get(f"/reviews/{assessment_id}").text

    assert "제거된 주장이 있다" in body
    assert "NEEDS_REVIEW" in body
    assert "소재만 겹친다" in body, "판정 이유가 없으면 검토자가 판정을 확인할 수 없다"
    assert "간접 도출" in body, "부서 근거가 영향 문단이 아니라는 사실이 보여야 한다"
    assert "QUOTE_NOT_FOUND" in body, "gate 가 무엇을 폐기했는지 보여야 한다"
    assert "재작성 1회" in body, "재작성 사실이 보이지 않으면 약해진 주장을 알아챌 수 없다"
    assert "주장이 약해져서" in body


async def test_decision_is_refused_when_graph_did_nothing(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """**재개가 승인 노드에 닿지 않으면 409 다.**

    200 을 돌려주면 화면은 승인됐다고 표시하고 실제로는 아무 일도 없다. 그리고 이 테스트가
    통과한다는 것은 **API 가 스스로 승인 레코드를 쓰지 않는다**는 뜻이다 — 썼다면
    `review_state` 가 바뀌어 200 이 나왔을 것이다.
    """
    assessment_id = await _seed(owner_conn)
    calls: list[str] = []

    async def resume(thread: str, _decision: dict[str, Any]) -> dict[str, Any]:
        calls.append(thread)
        return {}  # 그래프가 중단 상태가 아니었다

    with _client(resume) as client:
        response = client.post(
            f"/api/reviews/{assessment_id}/decision",
            json={"decision": "ACCEPT", "decided_by": "reviewer", "reviewed_ms": 1000},
        )

    assert response.status_code == 409
    assert "반영되지 않았다" in response.json()["detail"]
    assert calls == [str(assessment_id)], "그래프 재개를 시도하긴 해야 한다"


async def test_decision_succeeds_when_graph_records_it(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """그래프가 승인 레코드를 만들면 API 는 그 결과를 그대로 전달한다."""
    assessment_id = await _seed(owner_conn)
    review_conn = await role_connect(DbRole.REVIEW)

    async def resume(_thread: str, decision: dict[str, Any]) -> dict[str, Any]:
        decision_id, outbox = await record_decision(
            review_conn,
            assessment_id=assessment_id,
            request=DecisionRequest.model_validate(decision),
        )
        await review_conn.commit()
        return {"decision_id": str(decision_id), "outbox_ids": [str(i) for i in outbox]}

    with _client(resume) as client:
        response = client.post(
            f"/api/reviews/{assessment_id}/decision",
            json={"decision": "ACCEPT", "decided_by": "reviewer", "reviewed_ms": 2500},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["review_state"] == "ACCEPTED"
    assert len(body["outbox_ids"]) == 1


async def test_already_decided_is_conflict(
    owner_conn: psycopg.AsyncConnection[Any], role_connect: Any
) -> None:
    """이미 결정된 건은 그래프를 부르기 전에 409 다."""
    assessment_id = await _seed(owner_conn)
    review_conn = await role_connect(DbRole.REVIEW)
    await record_decision(
        review_conn,
        assessment_id=assessment_id,
        request=DecisionRequest(
            decision=ReviewDecisionKind.ACCEPT, decided_by="reviewer", reviewed_ms=1
        ),
    )
    await review_conn.commit()

    async def resume(_thread: str, _decision: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("이미 결정된 건에 그래프를 재개하면 안 된다")

    with _client(resume) as client:
        response = client.post(
            f"/api/reviews/{assessment_id}/decision",
            json={"decision": "REJECT", "decided_by": "other", "reviewed_ms": 1},
        )

    assert response.status_code == 409


async def test_unknown_assessment_is_not_found(owner_conn: psycopg.AsyncConnection[Any]) -> None:  # noqa: ARG001
    """없는 평가는 404 다. 권한 없음과 부재를 뭉뚱그리지 않는다."""

    async def resume(_thread: str, _decision: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("없는 평가에 그래프를 재개하면 안 된다")

    with _client(resume) as client:
        assert client.get(f"/api/reviews/{uuid4()}").status_code == 404
