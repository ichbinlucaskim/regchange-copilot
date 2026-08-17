"""접속 문자열 — role 별로 다른 DSN 을 고른다.

목적:
    프로세스가 자기 역할에 맞는 role 로 접속하도록 DSN 을 한 곳에서 만든다.

구현 이유:
    DSN 을 호출부마다 조립하면 급할 때 소유자 계정으로 붙는 코드가 하나 생기고,
    그 하나가 원칙 5의 경계를 통째로 무력화한다. 권한 경계는 DB 에 있지만
    **어느 role 로 붙을지는 애플리케이션이 정하므로**, 그 선택을 한 곳에 모은다.

    환경변수 이름을 role 이름에서 기계적으로 만든다(`DATABASE_URL_APP_INGEST`).
    role 을 추가할 때 이 파일을 고치지 않아도 되고, 이름을 지어낼 여지도 없다.

트레이드오프:
    로컬 기본값에 개발용 비밀번호가 코드에 들어 있다. 운영에서는 환경변수가
    반드시 주어져야 하며, 주어지지 않으면 로컬 기본값으로 조용히 떨어진다 —
    그 경우 접속 자체가 실패하므로 조용한 실패는 아니다. 운영 DSN 관리는
    `adapters/secrets.py` 경계의 일이며 이 모듈이 다루지 않는다.

엣지 케이스:
    - 알 수 없는 role 이름: `KeyError` 가 아니라 그대로 환경변수를 찾고, 없으면
      로컬 규칙으로 조립한다. role 이 DB 에 없으면 접속에서 실패한다.
    - `DATABASE_URL` 만 주어진 경우: 소유자 접속에만 쓴다. role 별 DSN 을 여기서
      파생시키지 않는다 — 비밀번호를 추측하게 되기 때문이다.
"""

from __future__ import annotations

import os
from enum import StrEnum

LOCAL_HOST = "localhost"
LOCAL_PORT = 5433
"""docker-compose 가 호스트에 노출하는 포트. 5432 와 충돌하지 않도록 옮겨 놓았다."""

LOCAL_DATABASE = "regchange"
OWNER_DSN_ENV = "DATABASE_URL"
DEFAULT_OWNER_DSN = (
    f"postgresql://regchange:regchange_local_dev_only@{LOCAL_HOST}:{LOCAL_PORT}/{LOCAL_DATABASE}"
)

CONNECT_TIMEOUT_SECONDS = 3
"""접속 불가를 빨리 확정하기 위한 값. 로컬 DB 이므로 짧게 둔다."""


class DbRole(StrEnum):
    """접속 role 4종. 이름은 `003_roles_and_grants.sql` 과 같아야 한다."""

    INGEST = "app_ingest"
    GRAPH = "app_graph"
    REVIEW = "app_review"
    DISPATCH = "app_dispatch"


def owner_dsn() -> str:
    """스키마 소유자 DSN. 마이그레이션에만 쓴다."""
    return os.environ.get(OWNER_DSN_ENV, DEFAULT_OWNER_DSN)


def role_env_var(role: DbRole) -> str:
    """환경변수 이름을 role 이름에서 만든다. `app_ingest` → `DATABASE_URL_APP_INGEST`."""
    return f"{OWNER_DSN_ENV}_{role.value.upper()}"


def role_dsn(role: DbRole) -> str:
    """접속 문자열을 role 별로 고른다. 환경변수가 있으면 그것을 쓰고, 없으면 로컬 규칙으로 조립한다.

    로컬 비밀번호 규칙은 `003_roles_and_grants.sql` 이 만든 값과 같아야 한다.
    두 곳에 같은 문자열이 있는 것은 중복이지만, 마이그레이션이 SQL 이므로
    한쪽을 다른 쪽에서 읽어올 수 없다. 대신 security 테스트가 4종 전부로 실제
    접속을 시도하므로, 어긋나면 즉시 드러난다.
    """
    override = os.environ.get(role_env_var(role))
    if override:
        return override
    return (
        f"postgresql://{role.value}:{role.value}_local_dev_only"
        f"@{LOCAL_HOST}:{LOCAL_PORT}/{LOCAL_DATABASE}"
    )
