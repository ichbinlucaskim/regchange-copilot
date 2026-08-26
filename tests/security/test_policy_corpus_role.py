"""사내 규정 코퍼스 테이블의 권한 경계를 DB 에 물어서 확인한다 (원칙 5, 마이그레이션 009).

이 테스트가 막는 위협:
    - 법령 수집 경로(app_ingest)가 사내 규정 문서를 읽는 것. 직무분리이며, 읽을 수
      있으면 "여기서 같이 처리하면 편한데"가 반드시 생긴다 (축 2)
    - LLM 경로(app_graph)가 검색 대상 코퍼스를 **쓰는** 것. 프롬프트 인젝션이
      성립하더라도 사내 규정 원문을 바꿀 수 없어야 한다
    - 규정 적재 경로(app_policy)가 법령 테이블이나 발송 outbox 로 시야를 넓히는 것
    - 발송 워커(app_dispatch)가 규정 원문을 보는 것
    - 적재된 문단을 사후에 UPDATE 로 고치는 것 (원칙 6 — 인용이 가리키는 원문이
      조용히 바뀌면 6개월 뒤 감사에서 "그때 무엇을 보고 판단했는가"에 답할 수 없다)
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

pytestmark = [pytest.mark.security, pytest.mark.requires_db]

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)

INSERT_DOCUMENT = """
INSERT INTO policy_document (
    id, doc_id, version, title, owner_dept, classification,
    effective_date, source_path, source_sha256, known_from
) VALUES (%s, %s, '1.0', 't', '정보보호부', 'INTERNAL',
          DATE '2025-01-01', 'p.md', repeat('a', 64), %s)
"""

INSERT_PARAGRAPH = """
INSERT INTO policy_paragraph (
    id, document_id, article_no, article_title, seq_in_doc,
    text_raw, text_norm, text_norm_sha256, norm_rule_version, known_from
) VALUES (%s, %s, 1, '목적', 1, '원문', '원문', repeat('b', 64), 'norm-v2', %s)
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
# app_ingest — 법령 수집. 사내 규정은 존재조차 몰라야 한다
# ---------------------------------------------------------------------------


async def test_ingest_role_cannot_read_policy_document(role_connect: RoleConnect) -> None:
    """수집 경로는 사내 규정 문서를 읽을 수 없다 (직무분리)."""
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(conn, "SELECT count(*) FROM policy_document")


async def test_ingest_role_cannot_read_policy_paragraph(role_connect: RoleConnect) -> None:
    """수집 경로는 사내 규정 문단도 읽을 수 없다."""
    conn = await role_connect(DbRole.INGEST)
    await _expect_denied(conn, "SELECT count(*) FROM policy_paragraph")


# ---------------------------------------------------------------------------
# app_graph — LLM 경로. 코퍼스를 읽되 쓰지 못한다
# ---------------------------------------------------------------------------


async def test_graph_role_can_read_corpus(role_connect: RoleConnect) -> None:
    """LLM 경로는 검색 대상 코퍼스를 읽을 수 있어야 한다. 못 읽으면 검색이 성립하지 않는다."""
    conn = await role_connect(DbRole.GRAPH)
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM policy_paragraph")
        assert await cur.fetchone() is not None
        await cur.execute("SELECT count(*) FROM policy_paragraph_embedding")
        assert await cur.fetchone() is not None


async def test_graph_role_cannot_insert_policy_paragraph(role_connect: RoleConnect) -> None:
    """LLM 경로가 사내 규정 문단을 만들 수 없다. 인젝션이 성립해도 피해가 조회에 갇힌다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(conn, INSERT_PARAGRAPH, (uuid4(), uuid4(), NOW))


async def test_graph_role_cannot_write_embedding(role_connect: RoleConnect) -> None:
    """LLM 경로가 임베딩을 바꿀 수 없다. 벡터를 바꾸면 검색 결과를 조종할 수 있다."""
    conn = await role_connect(DbRole.GRAPH)
    await _expect_denied(
        conn,
        "INSERT INTO policy_paragraph_embedding (paragraph_id, model_id, dim, embedding)"
        " VALUES (%s, 'm', 3, '[1,2,3]'::vector)",
        (uuid4(),),
    )


# ---------------------------------------------------------------------------
# app_policy — 규정 적재. 시야가 코퍼스 밖으로 넓어지지 않는다
# ---------------------------------------------------------------------------


async def test_policy_role_can_load_corpus(role_connect: RoleConnect) -> None:
    """적재 role 은 문서와 문단을 넣을 수 있어야 한다. 못 넣으면 코퍼스가 만들어지지 않는다."""
    conn = await role_connect(DbRole.POLICY)
    document_id, paragraph_id = uuid4(), uuid4()
    async with conn.cursor() as cur:
        await cur.execute(INSERT_DOCUMENT, (document_id, f"ISP-SEC-{document_id.hex[:6]}", NOW))
        await cur.execute(INSERT_PARAGRAPH, (paragraph_id, document_id, NOW))
    await conn.rollback()


async def test_policy_role_cannot_read_regulation(role_connect: RoleConnect) -> None:
    """규정 적재가 법령을 읽을 이유가 없다. 읽히면 두 경로가 한 프로세스로 합쳐진다."""
    conn = await role_connect(DbRole.POLICY)
    await _expect_denied(conn, "SELECT count(*) FROM regulation_article")


async def test_policy_role_cannot_read_outbox(role_connect: RoleConnect) -> None:
    """규정 적재가 발송 outbox 를 볼 수 없다."""
    conn = await role_connect(DbRole.POLICY)
    await _expect_denied(conn, "SELECT count(*) FROM action_outbox")


async def test_policy_role_cannot_update_paragraph(role_connect: RoleConnect) -> None:
    """적재 role 에 UPDATE 가 없다.

    정정은 운영 절차이지 적재 경로의 권한이 아니다 — 003 이 app_ingest 에 UPDATE 를
    주지 않은 것과 같은 판단이다.
    """
    conn = await role_connect(DbRole.POLICY)
    await _expect_denied(conn, "UPDATE policy_paragraph SET text_raw = 'x'")


# ---------------------------------------------------------------------------
# app_dispatch — 발송 워커. 규정 원문을 보지 않는다 (원칙 5)
# ---------------------------------------------------------------------------


async def test_dispatch_role_cannot_read_corpus(role_connect: RoleConnect) -> None:
    """발송 워커는 승인 레코드에서 파생된 outbox 만 본다."""
    conn = await role_connect(DbRole.DISPATCH)
    await _expect_denied(conn, "SELECT count(*) FROM policy_paragraph")


# ---------------------------------------------------------------------------
# 불변성 — 인용이 가리키는 원문은 사후에 바뀌지 않는다 (원칙 6)
# ---------------------------------------------------------------------------


async def test_paragraph_text_cannot_be_updated(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """소유자 계정으로도 문단 원문을 UPDATE 할 수 없다.

    권한이 아니라 트리거가 막는다. 권한은 role 을 바꾸면 우회되지만 트리거는 그렇지 않다.
    인용이 가리키는 원문이 조용히 바뀌면 "그 시점에 무엇을 보고 판단했는가"에 답할 수 없다.
    """
    document_id, paragraph_id = uuid4(), uuid4()
    async with owner_conn.cursor() as cur:
        await cur.execute(INSERT_DOCUMENT, (document_id, f"ISP-SEC-{document_id.hex[:6]}", NOW))
        await cur.execute(INSERT_PARAGRAPH, (paragraph_id, document_id, NOW))

        with pytest.raises(psycopg.DatabaseError) as caught:
            await cur.execute(
                "UPDATE policy_paragraph SET text_raw = 'changed' WHERE id = %s",
                (paragraph_id,),
            )
        assert "UPDATE 할 수 없다" in str(caught.value)
    await owner_conn.rollback()


async def test_paragraph_cannot_be_deleted(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """문단을 지울 수 없다. 코퍼스에서 사라진 문단을 가리키는 과거 인용은 검증 불가능해진다."""
    document_id, paragraph_id = uuid4(), uuid4()
    async with owner_conn.cursor() as cur:
        await cur.execute(INSERT_DOCUMENT, (document_id, f"ISP-SEC-{document_id.hex[:6]}", NOW))
        await cur.execute(INSERT_PARAGRAPH, (paragraph_id, document_id, NOW))

        with pytest.raises(psycopg.DatabaseError) as caught:
            await cur.execute("DELETE FROM policy_paragraph WHERE id = %s", (paragraph_id,))
        assert "DELETE 할 수 없다" in str(caught.value)
    await owner_conn.rollback()
