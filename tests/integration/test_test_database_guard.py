"""테스트가 실제로 어느 DB 에 붙어 있는가 — 사고 재발 방지의 배선 확인.

이 테스트가 존재하는 이유: `assert_disposable_database` 가 옳게 동작하는 것과
**그 함수가 파괴 경로에 실제로 걸려 있는 것**은 다른 사실이다. 단위 테스트는 앞의
것만 증명한다. 여기서는 뒤의 것을 본다 —

  1. 픽스처가 건네주는 커넥션은 정말 `_test` DB 를 보고 있는가
  2. 운영 DB 의 **실제 이름**은 그 가드를 통과하지 못하는가

2번이 없으면 1번은 "가드가 아무것도 막지 않는데 이름만 맞았다"와 구별되지 않는다.

사건: `docs/incidents/test-truncated-operations-history.md`
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from regchange.store.dsn import (
    CONNECT_TIMEOUT_SECONDS,
    DISPOSABLE_DATABASE_SUFFIX,
    ProductionDatabaseError,
    assert_disposable_database,
    owner_dsn,
)

pytestmark = pytest.mark.requires_db


async def test_fixtures_hand_out_a_disposable_database(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """`owner_conn` 이 TRUNCATE 하는 대상이 운영 DB 가 아니어야 한다."""
    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT current_database()")
        row = await cur.fetchone()

    assert row is not None
    assert str(row[0]).endswith(DISPOSABLE_DATABASE_SUFFIX)


async def test_the_operations_database_would_be_rejected() -> None:
    """**운영 DB 에 붙으면 가드가 발화한다.**

    지우지 않는다 — 접속해서 이름만 묻고, 그 이름이 가드를 통과하지 못하는 것을
    확인한다. 사고 당시 이 경로에 아무 검사도 없었다.
    """
    try:
        conn = await psycopg.AsyncConnection.connect(
            owner_dsn(), connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres 에 접속할 수 없다 (make up 필요): {exc}")

    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT current_database()")
            row = await cur.fetchone()
        assert row is not None
        with pytest.raises(ProductionDatabaseError):
            assert_disposable_database(str(row[0]))
    finally:
        await conn.close()
