"""DB 를 쓰는 테스트의 공용 픽스처.

이 파일이 존재하는 이유: bitemporal 테이블은 UPDATE/DELETE 가 트리거로 막혀 있어
테스트가 평범한 방법으로 정리할 수 없다. 정리 방법을 각 테스트가 알아서 정하면
그중 하나가 트리거를 우회하는 길을 찾아내고, 그 길이 곧 운영 코드로 새어 나간다.
정리는 여기 한 곳에서만 한다.

`TRUNCATE` 를 쓴다. 행 단위 BEFORE DELETE 트리거가 발화하지 않기 때문이다. 이것은
불변성의 구멍처럼 보이지만 `TRUNCATE` 는 테이블 소유자 권한을 요구하고 app_* role
어디에도 부여하지 않았다 — 그 사실 자체를 security 테스트가 검사한다.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg
import pytest

from regchange.store.dsn import CONNECT_TIMEOUT_SECONDS, DbRole, owner_dsn, role_dsn
from regchange.store.migrate import apply_migrations

TRUNCATED_TABLES = (
    "regulation_article",
    "regulation_document",
    "ministry_unresolved",
    "load_run",
    "article_change",
    "article_move_candidate",
    "change_set",
)
"""테스트마다 비우는 테이블.

`ministry_master` 는 비우지 않는다 — 마이그레이션 004 가 넣은 시드가 테스트의
입력이고, 비우면 매번 다시 넣어야 하며 그 재삽입이 시드와 어긋날 수 있다.
"""

FIXED_NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)
"""테스트가 쓰는 고정 시각. 적재 시각이 흔들리면 bitemporal 단언이 흔들린다."""


@pytest.fixture
async def owner_conn() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """스키마 소유자 커넥션. 마이그레이션을 적용하고 테이블을 비운 상태로 준다."""
    try:
        conn = await psycopg.AsyncConnection.connect(
            owner_dsn(), connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres 에 접속할 수 없다 (make up 필요): {exc}")

    try:
        await apply_migrations(conn)
        async with conn.cursor() as cur:
            await cur.execute(f"TRUNCATE {', '.join(TRUNCATED_TABLES)} CASCADE")
        await conn.commit()
        yield conn
    finally:
        await conn.close()


RoleConnect = Callable[[DbRole], Awaitable[psycopg.AsyncConnection[Any]]]


@pytest.fixture
async def role_connect(
    owner_conn: psycopg.AsyncConnection[Any],  # noqa: ARG001 — 마이그레이션·정리를 먼저 끝내기 위한 의존
) -> AsyncIterator[RoleConnect]:
    """role 별 커넥션을 여는 팩토리.

    `owner_conn` 에 의존하는 이유는 순서 때문이다 — role 은 마이그레이션 003 이
    만들므로, 마이그레이션이 끝나기 전에 접속하면 role 이 없어서 실패한다.
    그 실패는 "권한이 없다"와 구별되지 않아 보안 테스트를 오독하게 만든다.
    """
    opened: list[psycopg.AsyncConnection[Any]] = []

    async def _connect(role: DbRole) -> psycopg.AsyncConnection[Any]:
        conn = await psycopg.AsyncConnection.connect(
            role_dsn(role), connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
        opened.append(conn)
        return conn

    try:
        yield _connect
    finally:
        for conn in opened:
            await conn.close()
