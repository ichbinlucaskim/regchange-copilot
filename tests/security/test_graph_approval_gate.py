"""승인 없는 실행 경로가 **존재하지 않는지** 세 겹으로 증명한다 (원칙 4, 원칙 5).

이 테스트가 막는 위협:
    - 그래프에 승인을 거치지 않고 발송 대상을 만드는 간선이 추가되는 것
    - LLM 이 관여하는 프로세스(app_graph)가 승인 레코드나 발송 대상을 쓰는 것
    - 권한 설정이 잘못돼도 승인 없는 발송 대상 행이 만들어지는 것 (FK)
    - 발송 워커(app_dispatch)가 체크포인트의 프롬프트·모델 출력을 보는 것
    - 초안 본문이 사후에 바뀌는 것 (원칙 6)

**세 겹인 이유**: 그래프 구조는 코드 수정으로 무너지고, 권한은 role 설정으로 무너진다.
FK 는 마이그레이션이어야 바뀐다. 무너지는 방식이 다른 방어를 겹쳐 둔다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest

from regchange.graph.build import build_graph
from regchange.graph.nodes import GraphDeps
from regchange.guards.killswitch import Switch, static_gate
from regchange.store.dsn import DbRole

pytestmark = pytest.mark.security

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)

WRITE_NODES = {"enqueue_actions", "record_rejection"}
"""승인 이후에만 도는 노드. 이 둘만 쓰기 커넥션을 본다."""


def _edges() -> list[tuple[str, str]]:
    """컴파일된 그래프의 간선. 커넥션은 실제로 쓰이지 않으므로 대역으로 채운다."""
    deps = GraphDeps(
        graph_conn=MagicMock(),
        review_conn=MagicMock(),
        switches=static_gate({Switch.RETRIEVAL: True, Switch.LLM: True}),
        llm=MagicMock(),
        embedding=MagicMock(),
        store=MagicMock(),
        as_of=dt.date(2026, 2, 1),
    )
    return [(e.source, e.target) for e in build_graph(deps).get_graph().edges]


# ---------------------------------------------------------------------------
# 1겹 — 그래프 구조
# ---------------------------------------------------------------------------


def test_write_nodes_are_only_reachable_from_human_review() -> None:
    """**승인 노드를 거치지 않고 발송 대상에 도달하는 간선이 없다.**

    이것이 원칙 4 의 실체다. UI 검사는 API 를 직접 호출하면 우회되지만, 그래프에 다른
    경로가 없으면 우회할 대상 자체가 없다.
    """
    incoming: dict[str, set[str]] = {}
    for source, target in _edges():
        incoming.setdefault(target, set()).add(source)

    for node in WRITE_NODES:
        assert incoming.get(node) == {"human_review"}, (
            f"{node} 로 들어오는 간선이 human_review 외에 있다: {incoming.get(node)}"
        )


def test_human_review_is_actually_in_the_graph() -> None:
    """승인 노드 자체가 사라지지 않았는지 확인한다.

    위 테스트는 `enqueue_actions` 가 없어도 통과한다 — 없는 노드에는 들어오는 간선도
    없기 때문이다. 노드의 존재를 따로 고정한다.
    """
    nodes = {source for source, _ in _edges()} | {target for _, target in _edges()}

    assert "human_review" in nodes
    assert nodes >= WRITE_NODES


def test_no_bypass_edge_from_persist_to_dispatch() -> None:
    """근거 부족 경로가 발송 대상으로 이어지지 않는다.

    `persist_assessment` 는 사람에게 가거나 끝난다. 이 두 갈래 외의 간선이 생기면
    "근거가 부족한데 발송된다"가 성립할 수 있다.
    """
    targets = {target for source, target in _edges() if source == "persist_assessment"}

    assert targets == {"human_review", "__end__"}


# ---------------------------------------------------------------------------
# 2겹 — DB 권한. LLM 경로는 승인과 발송 대상을 쓸 수 없다
# ---------------------------------------------------------------------------


async def _expect_denied(
    conn: psycopg.AsyncConnection[Any], sql: str, params: tuple[Any, ...]
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
    await conn.rollback()


@pytest.mark.requires_db
async def test_graph_role_cannot_write_review_decision(role_connect: Any) -> None:
    """**LLM 경로는 승인 레코드를 만들 수 없다.** 원칙 5 의 경계가 여기 있다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(
        conn,
        """
        INSERT INTO review_decision (
            id, impact_assessment_id, decided_by, decision, decided_at, reviewed_ms
        ) VALUES (%s, %s, 'x', 'ACCEPT', %s, 0)
        """,
        (uuid4(), uuid4(), NOW),
    )


@pytest.mark.requires_db
async def test_graph_role_cannot_write_action_outbox(role_connect: Any) -> None:
    """**LLM 경로는 발송 대상을 만들 수 없다.** 인젝션이 성립해도 피해가 조회에 갇힌다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(
        conn,
        """
        INSERT INTO action_outbox (id, review_decision_id, action_type, payload, created_at)
        VALUES (%s, %s, 'X', '{}'::jsonb, %s)
        """,
        (uuid4(), uuid4(), NOW),
    )


@pytest.mark.requires_db
async def test_dispatch_role_cannot_read_checkpoints(role_connect: Any) -> None:
    """발송 워커는 체크포인트를 볼 수 없다. 거기에 프롬프트와 모델 출력이 있다 (원칙 5)."""
    conn = await role_connect(DbRole.DISPATCH)
    with pytest.raises(psycopg.errors.Error):
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM graph_checkpoint.checkpoints LIMIT 1")
    await conn.rollback()


@pytest.mark.requires_db
async def test_dispatch_role_cannot_read_assessments(role_connect: Any) -> None:
    """발송 워커는 초안도 볼 수 없다. 승인 레코드에서 파생된 outbox 만 본다."""
    conn = await role_connect(DbRole.DISPATCH)
    await _expect_denied(conn, "SELECT 1 FROM impact_assessment LIMIT %s", (1,))


# ---------------------------------------------------------------------------
# 3겹 — 제약. 권한이 잘못 주어져도 남는다
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
async def test_outbox_requires_a_decision(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """**승인 레코드 없이 발송 대상이 존재할 수 없다.**

    소유자 권한으로도 만들 수 없다. 권한은 role 설정으로 무너지지만 FK 는 마이그레이션이
    있어야 바뀐다 — 무너지는 방식이 다른 방어를 겹쳐 둔 자리다.
    """
    with pytest.raises(psycopg.errors.NotNullViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO action_outbox (id, review_decision_id, action_type, payload, created_at)
                VALUES (%s, NULL, 'X', '{}'::jsonb, %s)
                """,
                (uuid4(), NOW),
            )
    await owner_conn.rollback()


@pytest.mark.requires_db
async def test_assessment_body_cannot_be_changed(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """초안 본문은 사후에 바뀌지 않는다. 검토자의 수정은 승인 레코드에 남는다 (원칙 6)."""
    assessment_id = uuid4()
    async with owner_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO impact_assessment (
                id, thread_id, created_at, law_name, article_path, revision_kind,
                change_type, as_of, status, obligation_type, risk_level, confidence,
                summary, reason, draft_json, queued_at, due_at, review_state
            ) VALUES (
                %s, 't', %s, '법', '제1조', '일부개정', 'MODIFIED', DATE '2026-02-01',
                'OK', 'NEW', 'HIGH', 'MEDIUM', '요약', '사유', '{}'::jsonb, %s, %s, 'PENDING'
            )
            """,
            (assessment_id, NOW, NOW, NOW),
        )
    await owner_conn.commit()

    # review_state 만 바꿀 수 있다.
    async with owner_conn.cursor() as cur:
        await cur.execute(
            "UPDATE impact_assessment SET review_state = 'ACCEPTED' WHERE id = %s",
            (assessment_id,),
        )
    await owner_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE impact_assessment SET summary = '고침' WHERE id = %s", (assessment_id,)
            )
    await owner_conn.rollback()

    with pytest.raises(psycopg.errors.RaiseException):
        async with owner_conn.cursor() as cur:
            await cur.execute("DELETE FROM impact_assessment WHERE id = %s", (assessment_id,))
    await owner_conn.rollback()


@pytest.mark.requires_db
async def test_rejection_requires_a_reason_code(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """반려에는 사유 코드가 필수다. 없으면 반려율을 조치로 이을 수 없다 (F-7)."""
    assessment_id = uuid4()
    async with owner_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO impact_assessment (
                id, thread_id, created_at, law_name, article_path, revision_kind,
                change_type, as_of, status, obligation_type, risk_level, confidence,
                summary, reason, draft_json, queued_at, due_at, review_state
            ) VALUES (
                %s, 't', %s, '법', '제1조', '일부개정', 'MODIFIED', DATE '2026-02-01',
                'OK', 'NEW', 'HIGH', 'MEDIUM', '요약', '사유', '{}'::jsonb, %s, %s, 'PENDING'
            )
            """,
            (assessment_id, NOW, NOW, NOW),
        )
    await owner_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO review_decision (
                    id, impact_assessment_id, decided_by, decision, decided_at, reviewed_ms
                ) VALUES (%s, %s, 'x', 'REJECT', %s, 10)
                """,
                (uuid4(), assessment_id, NOW),
            )
    await owner_conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO review_decision (
                    id, impact_assessment_id, decided_by, decision, decided_at,
                    reason_code, reviewed_ms
                ) VALUES (%s, %s, 'x', 'REJECT', %s, 'OTHER', 10)
                """,
                (uuid4(), assessment_id, NOW),
            )
    await owner_conn.rollback()
