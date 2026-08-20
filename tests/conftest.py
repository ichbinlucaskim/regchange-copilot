"""DB 를 쓰는 테스트의 공용 픽스처.

이 파일이 존재하는 이유: bitemporal 테이블은 UPDATE/DELETE 가 트리거로 막혀 있어
테스트가 평범한 방법으로 정리할 수 없다. 정리 방법을 각 테스트가 알아서 정하면
그중 하나가 트리거를 우회하는 길을 찾아내고, 그 길이 곧 운영 코드로 새어 나간다.
정리는 여기 한 곳에서만 한다.

`TRUNCATE` 를 쓴다. 행 단위 BEFORE DELETE 트리거가 발화하지 않기 때문이다. 이것은
불변성의 구멍처럼 보이지만 `TRUNCATE` 는 테이블 소유자 권한을 요구하고 app_* role
어디에도 부여하지 않았다 — 그 사실 자체를 security 테스트가 검사한다.

**테스트는 별도 데이터베이스에서 돈다** (`regchange_test`). 같은 DB 를 쓰면
`make test` 한 번이 그날까지의 운영 이력(`ops_run`, `load_run`)을 통째로 지운다.
코드는 다시 쓸 수 있지만 "N개월간 매일 돌았다"는 다시 만들 수 없다 (ADR-014).
DB 를 나누는 것이 그 자산을 지키는 가장 싼 방법이다.

**권한 설정은 여기서 하지 않는다.** 테스트 DB 의 role·GRANT 는 마이그레이션 003 이
`current_database()` 로 부여한다 — conftest 가 권한을 흉내 내면 security 테스트가
검사하는 대상이 커밋된 SQL 이 아니라 테스트 코드가 되고, 그 순간 그 테스트는
아무것도 보증하지 않는다.

**그리고 분리를 믿지 않는다.** DB 를 나눈 것은 설정이고, 설정은 `.env` 한 줄이나
환경변수 하나로 되돌아온다. `TRUNCATE` 직전에 **서버에 접속 대상을 물어**
(`SELECT current_database()`) `_test` 로 끝나는지 확인하고 아니면 중단한다.
의도("테스트용 DSN 을 썼다")가 아니라 접속 결과를 검사하는 것이 요점이다 —
사건의 원인이 바로 그 의도와 결과의 어긋남이었다
(`docs/incidents/test-truncated-operations-history.md`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from regchange.store.dsn import (
    CONNECT_TIMEOUT_SECONDS,
    DbRole,
    assert_disposable_database,
    owner_dsn,
    role_dsn,
)
from regchange.store.migrate import apply_migrations

TEST_DATABASE = "regchange_test"
"""테스트 전용 데이터베이스. 운영 데이터가 있는 `regchange` 를 건드리지 않는다.

이름이 `_test` 로 끝나는 것은 규약이 아니라 **가드의 통과 조건**이다
(`DISPOSABLE_DATABASE_SUFFIX`). 이 상수를 바꾸려면 접미사를 지켜야 한다.
"""

TRUNCATED_TABLES = (
    "regulation_article",
    "regulation_document",
    "ministry_unresolved",
    "load_run",
    "article_change",
    "article_move_candidate",
    "change_set",
    "ops_law_outcome",
    "ops_run",
)
"""테스트마다 비우는 테이블.

`ministry_master` 는 비우지 않는다 — 마이그레이션 004 가 넣은 시드가 테스트의
입력이고, 비우면 매번 다시 넣어야 하며 그 재삽입이 시드와 어긋날 수 있다.
"""

FIXED_NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)
"""테스트가 쓰는 고정 시각. 적재 시각이 흔들리면 bitemporal 단언이 흔들린다."""


def in_test_database(dsn: str) -> str:
    """DSN 의 데이터베이스만 테스트용으로 바꾼다. 호스트·계정·비밀번호는 그대로다."""
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{assert_disposable_database(TEST_DATABASE)}"))


async def guard_disposable(conn: psycopg.AsyncConnection[Any]) -> str:
    """이 커넥션이 정말 비워도 되는 DB 를 보고 있는지 **서버에 물어서** 확인한다.

    DSN 문자열이 아니라 `current_database()` 를 쓴다. DSN 은 우리 의도이고 서버
    응답은 사실이며, 사건의 원인이 그 둘의 어긋남이었다.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT current_database()")
        row = await cur.fetchone()
    return assert_disposable_database("" if row is None else str(row[0]))


@pytest.fixture(scope="session")
def test_database() -> str:
    """테스트 DB 를 만들고(없으면) 그 DSN 을 돌려준다.

    스키마와 권한은 만들지 않는다 — `owner_conn` 이 마이그레이션을 적용하며,
    그 마이그레이션이 곧 운영에 나갈 SQL 이다.
    """
    admin = owner_dsn()
    try:
        with psycopg.connect(
            admin, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)
            ).fetchone()
            if exists is None:
                # 식별자는 바인딩할 수 없다. 값은 이 파일의 상수이며 외부 입력이 닿지 않는다.
                conn.execute(f'CREATE DATABASE "{TEST_DATABASE}"')
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres 에 접속할 수 없다 (make up 필요): {exc}")
    return in_test_database(admin)


@pytest.fixture
async def owner_conn(test_database: str) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """스키마 소유자 커넥션. 마이그레이션을 적용하고 테이블을 비운 상태로 준다."""
    try:
        conn = await psycopg.AsyncConnection.connect(
            test_database, connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres 에 접속할 수 없다 (make up 필요): {exc}")

    try:
        # 마이그레이션보다 먼저 검사한다. 운영 DB 에 붙었다면 스키마를 건드리기
        # 전에 멈춰야 한다 — 마이그레이션도 되돌릴 수 없는 작업이다.
        await guard_disposable(conn)
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
            in_test_database(role_dsn(role)), connect_timeout=CONNECT_TIMEOUT_SECONDS
        )
        opened.append(conn)
        return conn

    try:
        yield _connect
    finally:
        for conn in opened:
            await conn.close()
