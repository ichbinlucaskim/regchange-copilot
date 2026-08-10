"""LLM 프로세스용 DB role 이 실제로 쓰기를 하지 못하는지 검사한다 (원칙 5).

이 테스트가 막는 위협: 프롬프트 인젝션이나 코드 버그로 LLM 경로가 쓰기를 시도하는 것.
애플리케이션 조건문은 코드 수정으로 무너지지만 DB 권한은 그렇지 않다. 그 권한이
실제로 설정되어 있는지를 코드가 아니라 DB 에 물어서 확인한다.

DB 가 필요하므로 `make up` 없이는 skip 된다. skip 은 통과가 아니다 — CI 에서는
Postgres 를 띄운 상태로 실행한다 (CLAUDE.md §6).
"""

import os
from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.security, pytest.mark.requires_db]

DEFAULT_READONLY_URL = (
    "postgresql://regchange_llm_ro:regchange_ro_local_dev_only@localhost:5433/regchange"
)

CONNECT_TIMEOUT_SECONDS = 3
"""접속이 안 되는 상황을 빨리 skip 으로 확정하기 위한 값. 로컬 DB 이므로 짧게 둔다."""


def _readonly_connection() -> psycopg.Connection[Any]:
    url = os.environ.get("DATABASE_URL_READONLY", DEFAULT_READONLY_URL)
    try:
        return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg.OperationalError as exc:
        pytest.skip(f"읽기 전용 role 로 접속할 수 없다 (make up 필요): {exc}")


def test_pgvector_extension_is_installed() -> None:
    """pgvector 확장이 설치되어 있어야 retrieval 이 성립한다."""
    with _readonly_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()

    assert row is not None, "pgvector 확장이 설치되어 있지 않다"


def test_readonly_role_cannot_create_tables() -> None:
    """읽기 전용 role 은 스키마에 객체를 만들 수 없어야 한다."""
    with _readonly_connection() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("CREATE TABLE security_probe_should_fail (id int)")
        conn.rollback()


def test_readonly_role_cannot_write_to_catalog_visible_schema() -> None:
    """읽기 전용 role 은 임의 스키마 생성으로 우회할 수 없어야 한다."""
    with _readonly_connection() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("CREATE SCHEMA security_probe_should_fail")
        conn.rollback()
