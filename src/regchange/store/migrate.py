"""마이그레이션 적용 — 파일 순서대로, 한 번씩, 내용 해시를 대조하며.

목적:
    `db/migrations/*.sql` 을 파일명 순서로 적용하고 무엇이 적용됐는지 DB 에 남긴다.

구현 이유:
    Alembic 을 쓰지 않는다. 이 저장소의 스키마는 bitemporal 제약·트리거·컬럼 단위
    GRANT 처럼 SQL 로만 정확히 표현되는 것들이 중심이고, 그것들을 ORM 메타데이터로
    옮기면 **감사에 제출할 원본이 파이썬 코드**가 된다. "왜 이 UPDATE 가 막히는가"에
    답할 때 읽어야 할 것은 트리거 정의 그 자체다. 대신 자동 생성과 다운그레이드를
    포기한다.

    적용 기록에 **파일 내용의 sha256 을 함께 남긴다.** 이미 적용된 마이그레이션
    파일을 나중에 고치면 DB 상태와 파일이 어긋나는데, 그 어긋남은 아무 오류도 내지
    않고 다음 환경에서만 다른 스키마를 만든다. 해시를 대조하면 그 자리에서 실패한다.
    스냅샷 페이지를 sha256 으로 검증하는 것과 같은 발상이다.

트레이드오프:
    다운그레이드가 없다. 되돌리려면 새 마이그레이션을 쓴다. 원칙 6이 과거 레코드를
    지우지 않는 것과 같은 방향이며, 스키마 이력도 같은 성질로 다룬다.

    파일명 순서에 의존한다. `010_` 이전에 `002_` 를 끼워 넣으면 순서가 바뀐다.
    번호를 앞자리 0 으로 채워 사전순과 수치순을 일치시키는 것으로 대응한다.

엣지 케이스:
    - 이미 적용된 파일이 수정됨: `MigrationChecksumError`. 조용히 넘기지 않는다.
    - DB 에는 기록이 있는데 파일이 사라짐: 경고가 아니라 실패로 다룬다. 스키마의
      출처를 알 수 없는 상태이기 때문이다.
    - 파일이 하나도 없음: 실패로 다룬다. 디렉터리 경로가 틀렸을 가능성이 크고,
      "적용할 것이 없음"으로 통과시키면 빈 DB 위에서 테스트가 돌게 된다.
    - 중간 파일에서 실패: 그 파일의 트랜잭션만 롤백되고 앞선 파일은 적용된 채로
      남는다. 한 파일 = 한 트랜잭션이며, 부분 적용된 파일은 존재하지 않는다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

MIGRATIONS_DIRNAME = "db/migrations"
_FILENAME_PATTERN = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename   text PRIMARY KEY,
    sha256     char(64)    NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """마이그레이션 적용 실패."""


class MigrationChecksumError(MigrationError):
    """이미 적용된 마이그레이션 파일이 수정됐다."""


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """적용 대상 파일 하나."""

    filename: str
    sha256: str
    sql: str


def default_migrations_dir() -> Path:
    """저장소 루트 기준 `db/migrations`.

    이 파일 위치에서 위로 3단계(`store` → `regchange` → `src`)가 루트다.
    경로를 환경변수로 두지 않는 이유는, 틀린 경로가 "적용할 것이 없음"으로
    보이는 실패 모드를 하나 더 만들기 때문이다.
    """
    return Path(__file__).resolve().parents[3] / MIGRATIONS_DIRNAME


def load_migration_files(directory: Path | None = None) -> tuple[MigrationFile, ...]:
    """디렉터리에서 마이그레이션 파일을 읽어 파일명 순서로 돌려준다.

    엣지 케이스:
        - 이름 규칙(`NNN_snake_case.sql`)에 맞지 않는 파일: 실패로 다룬다.
          무시하면 적용되지 않은 마이그레이션이 조용히 생긴다.
        - 파일이 0건: `MigrationError`.
    """
    directory = directory or default_migrations_dir()
    if not directory.is_dir():
        raise MigrationError(f"마이그레이션 디렉터리가 없다: {directory}")

    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise MigrationError(
            f"마이그레이션 파일이 없다: {directory}. "
            "'적용할 것이 없음'으로 통과시키면 빈 스키마 위에서 테스트가 돈다"
        )

    files: list[MigrationFile] = []
    for path in paths:
        if not _FILENAME_PATTERN.match(path.name):
            raise MigrationError(
                f"마이그레이션 파일명 규칙 위반: {path.name} (NNN_snake_case.sql 이어야 한다)"
            )
        raw = path.read_bytes()
        files.append(
            MigrationFile(
                filename=path.name,
                sha256=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
            )
        )
    return tuple(files)


async def applied_migrations(conn: psycopg.AsyncConnection[Any]) -> dict[str, str]:
    """이미 적용된 파일명 → sha256."""
    async with conn.cursor() as cur:
        await cur.execute(_BOOTSTRAP_SQL)
        await cur.execute("SELECT filename, sha256 FROM schema_migration")
        rows = await cur.fetchall()
    await conn.commit()
    return {str(row[0]): str(row[1]) for row in rows}


async def apply_migrations(
    conn: psycopg.AsyncConnection[Any],
    directory: Path | None = None,
) -> tuple[str, ...]:
    """미적용 마이그레이션을 순서대로 적용하고 적용된 파일명을 돌려준다.

    목적:
        스키마를 파일이 기술하는 상태로 만든다.

    구현 이유:
        파일 하나를 한 트랜잭션으로 적용한다. 여러 파일을 한 트랜잭션에 묶으면
        중간 실패 시 어디까지 갔는지 알 수 없고, 파일보다 잘게 쪼개면 부분 적용된
        파일이 생긴다. 파일이 사람이 읽는 단위이므로 실패 단위도 파일이어야 한다.

    트레이드오프:
        `CREATE ROLE` 처럼 DB 전역에 영향을 주는 문장은 롤백돼도 다른 데이터베이스에
        남은 role 을 되돌리지 못한다. role 생성이 재실행 가능하게 쓰여 있어
        (duplicate_object 를 잡는다) 재적용은 안전하다.

    엣지 케이스:
        - 적용 기록이 있는데 해시가 다름: `MigrationChecksumError`.
        - 기록은 있는데 파일이 없음: `MigrationError`. 스키마 출처 불명 상태다.
        - 이미 전부 적용됨: 빈 튜플을 돌려준다. 실패가 아니다.
    """
    files = load_migration_files(directory)
    already = await applied_migrations(conn)

    known = {file.filename for file in files}
    missing = sorted(set(already) - known)
    if missing:
        raise MigrationError(
            f"적용 기록은 있는데 파일이 없다: {missing}. 현재 스키마의 출처를 확인할 수 없다"
        )

    applied: list[str] = []
    for file in files:
        recorded = already.get(file.filename)
        if recorded is not None:
            if recorded != file.sha256:
                raise MigrationChecksumError(
                    f"이미 적용된 마이그레이션이 수정됐다: {file.filename}\n"
                    f"  적용 당시 sha256: {recorded}\n"
                    f"  현재 파일 sha256: {file.sha256}\n"
                    "수정 대신 새 마이그레이션을 추가한다"
                )
            continue

        async with conn.cursor() as cur:
            await cur.execute(file.sql)
            await cur.execute(
                "INSERT INTO schema_migration (filename, sha256) VALUES (%s, %s)",
                (file.filename, file.sha256),
            )
        await conn.commit()
        applied.append(file.filename)

    return tuple(applied)
