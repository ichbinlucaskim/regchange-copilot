"""`python -m regchange.store` — 마이그레이션을 적용한다.

목적:
    `make migrate` 가 부를 진입점. 스키마를 파일이 기술하는 상태로 만든다.

구현 이유:
    CLI 프레임워크를 붙이지 않는다. 사용자용 CLI 는 별도 작업이며, 여기 필요한
    것은 인자 없는 운영 명령 하나뿐이다. 지금 프레임워크를 고르면 그 선택이
    CLI 설계를 먼저 확정해 버린다.

    결과를 `print` 가 아니라 구조화 로그로 낸다. 이 명령은 사람이 손으로도 돌리고
    배포 파이프라인에서도 돌므로, 출력이 두 소비자에게 같은 형태여야 한다.

트레이드오프:
    적용할 대상 디렉터리와 접속 대상을 인자로 받지 않는다. 둘 다 환경변수
    (`DATABASE_URL`)와 저장소 구조로 결정된다. 인자를 열면 "다른 디렉터리를 다른
    DB 에 적용"하는 경로가 생기고, 그 경로는 사고가 났을 때 무엇이 어디에
    적용됐는지 알 수 없게 만든다.

엣지 케이스:
    - 이미 전부 적용됨: `applied=[]` 로 성공 종료한다. 실패가 아니다.
    - 적용된 파일이 수정됨: `MigrationChecksumError` 로 비정상 종료한다.
    - DB 접속 불가: psycopg 예외가 그대로 올라간다. 감추지 않는다.
"""

from __future__ import annotations

import asyncio

import psycopg
import structlog

from regchange.store.dsn import owner_dsn
from regchange.store.migrate import apply_migrations

_log = structlog.get_logger(__name__)


async def _run() -> tuple[str, ...]:
    async with await psycopg.AsyncConnection.connect(owner_dsn()) as conn:
        return await apply_migrations(conn)


def main() -> None:
    """마이그레이션을 적용하고 무엇이 적용됐는지 로그로 남긴다."""
    applied = asyncio.run(_run())
    _log.info("store.migrated", applied=list(applied), count=len(applied))


if __name__ == "__main__":
    main()
