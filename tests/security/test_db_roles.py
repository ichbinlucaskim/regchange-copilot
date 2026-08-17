"""DB role 4종이 실제로 못 하는 일을 못 하는지 DB 에 물어서 확인한다 (원칙 5).

이 테스트가 막는 위협:
    - 프롬프트 인젝션이나 코드 버그로 LLM 경로(app_graph)가 규제 데이터를 쓰는 것
    - 수집 경로(app_ingest)가 사내 정책 문서나 발송 outbox 를 읽는 것 (직무분리)
    - 발송 워커(app_dispatch)가 프롬프트·모델 출력·규제 원문을 보는 것
    - 검토자(app_review)가 상태가 아니라 본문을 고치는 것
    - 어떤 role 이든 불변성 트리거를 TRUNCATE 로 우회하는 것

0.5단계 테스트와의 차이는 `test_readonly_db_role.py` 에 기록했다. 요약하면,
그 테스트는 "읽기 전용임"이 아니라 "스키마 소유자가 아님"만 증명했다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from regchange.store.dsn import DbRole

RoleConnect = Callable[[DbRole], Awaitable[psycopg.AsyncConnection[Any]]]
"""`conftest.role_connect` 픽스처의 타입. tests 는 패키지가 아니라 import 하지 않는다."""

pytestmark = [pytest.mark.security, pytest.mark.requires_db]

NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)

INSERT_DOCUMENT = """
INSERT INTO regulation_document (
    id, law_id, mst, law_name, document_effective_date,
    source_key, source_run_id, source_page_sha256, load_run_id, known_from
) VALUES (%s, '009244', '252787', '특금법', DATE '2024-01-01', 'k', 'r', %s, %s, %s)
"""


async def _expect_denied(
    conn: psycopg.AsyncConnection[Any], sql: str, params: tuple[Any, ...] = ()
) -> None:
    """이 문장이 권한 부족으로 거부되는지 확인한다."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
    await conn.rollback()


# ---------------------------------------------------------------------------
# app_graph — LLM 경로. 읽기만 하고 llm_invocation / audit_event 에만 쓴다
# ---------------------------------------------------------------------------


async def test_graph_role_cannot_insert_into_article_change(role_connect: RoleConnect) -> None:
    """LLM 경로는 diff 결과를 쓸 수 없다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(
        conn,
        "INSERT INTO article_change (id, change_type) VALUES (%s, 'AMENDED')",
        (uuid4(),),
    )


async def test_graph_role_cannot_update_regulation_article(role_connect: RoleConnect) -> None:
    """LLM 경로는 조문을 고칠 수 없다. 트리거 이전에 권한에서 막힌다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(conn, "UPDATE regulation_article SET title = 'x'")


async def test_graph_role_cannot_delete_regulation_document(role_connect: RoleConnect) -> None:
    """LLM 경로는 문서를 지울 수 없다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(conn, "DELETE FROM regulation_document")


async def test_graph_role_can_read_and_can_log_its_own_calls(role_connect: RoleConnect) -> None:
    """읽기와 자기 호출 기록은 되어야 한다 — 막기만 하면 경로가 성립하지 않는다."""
    conn = await role_connect(DbRole.GRAPH)
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM regulation_article")
        assert await cur.fetchone() is not None
        await cur.execute(
            "INSERT INTO llm_invocation (id, invoked_at, model) VALUES (%s, %s, 'test')",
            (uuid4(), NOW),
        )
        await cur.execute(
            "INSERT INTO audit_event (id, occurred_at, event_type) VALUES (%s, %s, 'TEST')",
            (uuid4(), NOW),
        )
    await conn.rollback()


# ---------------------------------------------------------------------------
# app_ingest — 수집·적재. 규제 테이블만 본다
# ---------------------------------------------------------------------------


async def test_ingest_role_cannot_read_policy_documents(role_connect: RoleConnect) -> None:
    """수집 경로는 사내 규정 문서를 읽을 이유가 없다 (직무분리, 축 2)."""
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(conn, "SELECT * FROM policy_document")


async def test_ingest_role_cannot_read_the_dispatch_outbox(role_connect: RoleConnect) -> None:
    """수집 경로는 발송 대기열을 볼 수 없다."""
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(conn, "SELECT * FROM action_outbox")


async def test_ingest_role_cannot_register_a_ministry_by_itself(
    role_connect: RoleConnect,
) -> None:
    """미지의 부처를 마스터에 자동 등재할 수 없다 (ADR-009).

    이 경계는 코드가 아니라 권한으로 세운다. 코드에 "자동 등재" 분기를 실수로
    넣어도 DB 가 거부한다 — 자동 병합을 금지한 결정이 코드 수정으로 무너지지
    않아야 한다.
    """
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(
        conn,
        """
        INSERT INTO ministry_master (id, org_code, org_name, source_field, source,
                                     valid_from, known_from)
        VALUES (%s, '9999999', '가상부', '소관부처명', 'OBSERVED_FLATTENED', DATE '2026-08-11', %s)
        """,
        (uuid4(), NOW),
    )


async def test_ingest_role_can_insert_regulation_rows(role_connect: RoleConnect) -> None:
    """적재는 되어야 한다."""
    conn = await role_connect(DbRole.INGEST)
    async with conn.cursor() as cur:
        await cur.execute(INSERT_DOCUMENT, (uuid4(), "0" * 64, uuid4(), NOW))
    await conn.rollback()


async def test_ingest_role_cannot_update_what_it_inserted(role_connect: RoleConnect) -> None:
    """적재 경로는 INSERT 만 한다. 정정은 운영 절차이며 별도 권한이다."""
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(conn, "UPDATE regulation_document SET law_name = 'x'")


# ---------------------------------------------------------------------------
# app_dispatch — 발송 워커. 승인에서 파생된 outbox 만 본다 (원칙 5)
# ---------------------------------------------------------------------------


async def test_dispatch_role_sees_only_the_outbox(role_connect: RoleConnect) -> None:
    """발송 워커는 규제 원문도 프롬프트도 볼 수 없다.

    "import 하지 않는다"로는 부족하다 — SQL 은 import 없이도 읽는다.
    """
    conn = await role_connect(DbRole.DISPATCH)
    await _expect_denied(conn, "SELECT * FROM regulation_article")
    await _expect_denied(conn, "SELECT * FROM llm_invocation")
    await _expect_denied(conn, "SELECT * FROM policy_document")


async def test_dispatch_role_can_work_the_outbox(role_connect: RoleConnect) -> None:
    """outbox 조회와 상태 갱신은 되어야 한다."""
    conn = await role_connect(DbRole.DISPATCH)
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM action_outbox")
        assert await cur.fetchone() is not None
        await cur.execute("UPDATE action_outbox SET state = 'SENT' WHERE state = 'PENDING'")
    await conn.rollback()


async def test_dispatch_role_cannot_create_its_own_work(role_connect: RoleConnect) -> None:
    """발송 워커가 발송 대상을 만들어낼 수 없다. 승인 레코드만 보고 동작한다."""
    conn = await role_connect(DbRole.DISPATCH)
    await _expect_denied(conn, "INSERT INTO action_outbox (id) VALUES (%s)", (uuid4(),))


# ---------------------------------------------------------------------------
# app_review — 검토자. 상태만 바꾼다 (원칙 4)
# ---------------------------------------------------------------------------


async def test_review_role_can_change_status_but_not_body(role_connect: RoleConnect) -> None:
    """컬럼 단위 권한 — 검토자는 상태를 바꾸고 본문은 고치지 않는다."""
    conn = await role_connect(DbRole.REVIEW)
    async with conn.cursor() as cur:
        await cur.execute("UPDATE impact_assessment SET status = 'APPROVED'")
    await conn.rollback()

    await _expect_denied(conn, "UPDATE impact_assessment SET body = 'x'")


async def test_review_role_cannot_write_regulation_data(role_connect: RoleConnect) -> None:
    """검토자는 규제 원문을 쓰지 않는다."""
    conn = await role_connect(DbRole.REVIEW)
    await _expect_denied(conn, INSERT_DOCUMENT, (uuid4(), "0" * 64, uuid4(), NOW))


# ---------------------------------------------------------------------------
# 불변성 우회 경로
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [DbRole.INGEST, DbRole.GRAPH, DbRole.REVIEW, DbRole.DISPATCH])
async def test_no_role_can_truncate_bitemporal_tables(
    role_connect: RoleConnect, role: DbRole
) -> None:
    """TRUNCATE 로 불변성 트리거를 우회할 수 없다.

    행 단위 BEFORE DELETE 트리거는 TRUNCATE 에 발화하지 않는다. 그래서 TRUNCATE 는
    DELETE 금지의 명백한 우회로이며, 어느 role 에도 그 권한을 주지 않았다는 것을
    검사한다. 테스트 정리에는 소유자 권한으로 TRUNCATE 를 쓰지만, 그 권한은
    애플리케이션 role 어디에도 없다.
    """
    conn = await role_connect(role)
    await _expect_denied(conn, "TRUNCATE regulation_article")


@pytest.mark.parametrize("role", [DbRole.INGEST, DbRole.GRAPH, DbRole.REVIEW, DbRole.DISPATCH])
async def test_every_role_can_actually_connect(role_connect: RoleConnect, role: DbRole) -> None:
    """4종 전부 실제로 접속된다.

    이 테스트가 없으면 DSN 규칙과 마이그레이션의 비밀번호가 어긋났을 때, 위의
    권한 테스트들이 접속 실패를 "거부됨"으로 오독할 수 있다.
    """
    conn = await role_connect(role)
    async with conn.cursor() as cur:
        await cur.execute("SELECT current_user")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == role.value
