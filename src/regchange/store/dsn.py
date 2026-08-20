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
from urllib.parse import urlsplit

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

DISPOSABLE_DATABASE_SUFFIX = "_test"
"""이 접미사로 끝나는 데이터베이스만 통째로 비울 수 있다.

**사건 기록**: `docs/incidents/test-truncated-operations-history.md`. 테스트의
`TRUNCATE` 가 운영 DB(`regchange`)를 비워 첫 운영 실행 기록이 소실됐다. DB 를
분리했지만 **분리는 설정이고 설정은 되돌아온다** — `.env` 한 줄이나 환경변수
하나면 다시 같은 일이 일어난다.

그래서 규칙을 이름에 건다. 파괴적 작업을 하려는 코드는 접속한 DB 의 이름을
서버에 물어 이 접미사를 확인하고, 아니면 **실행 전에 실패한다.**

접미사를 쓰는 이유는 목록(`{"regchange_test"}`)보다 확장되기 때문이다 —
`regchange_ci_test`, `regchange_pr123_test` 가 규칙만 지키면 자동으로 허용되고,
운영 DB 이름은 어떤 규칙으로도 이 접미사를 갖지 않는다.
"""


class ProductionDatabaseError(RuntimeError):
    """파괴적 작업을 운영 DB 에 하려 했다. 경고가 아니라 중단이다."""


def database_of(dsn: str) -> str:
    """접속 문자열에서 데이터베이스 이름만 꺼낸다.

    목적:
        DSN 파싱을 한 곳에 모은다. 문자열 `rsplit("/")` 이 호출부마다 흩어지면
        쿼리 파라미터(`?sslmode=`)가 붙은 DSN 에서 조용히 틀린다.

    구현 이유:
        `urlsplit` 을 쓴다. 경로만 보므로 쿼리·프래그먼트가 섞여도 안전하다.

    트레이드오프:
        `postgresql://` URL 형식만 다룬다. libpq 의 키워드 형식
        (`host=... dbname=...`)은 빈 문자열을 돌려준다 — 이 저장소는 URL 형식만
        쓰고, 빈 이름은 `assert_disposable_database` 에서 거부되므로 조용히
        통과하지 않는다.

    엣지 케이스:
        - 경로가 없는 DSN: 빈 문자열. "알 수 없음"이며 파괴 허용 대상이 아니다.
    """
    return urlsplit(dsn).path.lstrip("/")


def assert_disposable_database(database: str) -> str:
    """이 DB 를 비워도 되는지 확인한다. 아니면 `ProductionDatabaseError`.

    목적:
        `TRUNCATE`/`DROP` 같은 되돌릴 수 없는 작업 앞에 두는 마지막 관문.

    구현 이유:
        **이름으로 판정한다.** 접속 대상이 무엇인지 판단할 수 있는 값 중 서버가
        직접 알려 주는 것이 이름뿐이기 때문이다. 호출부가 "테스트 DSN 을 썼다"는
        의도를 근거로 삼으면, 그 의도가 환경변수 하나로 어긋나는 순간 방어가 사라진다.
        의도가 아니라 **접속 결과**(`SELECT current_database()`)를 검사해야 한다.

    트레이드오프:
        운영 DB 이름을 `_test` 로 지으면 방어가 무력해진다. 그 실수를 막을 방법은
        없지만, 그런 이름을 짓는 것 자체가 명시적 행위다 — 이 방어가 막으려는
        것은 실수이지 의도가 아니다.

        예외를 던지고 경고 로그로 넘기지 않는다. 이 함수가 발화하는 상황은
        "지금 무엇을 지우려는지 모르는 상태"이며, 모르는 채로 계속 가면 안 된다.

    엣지 케이스:
        - 빈 이름: 거부한다. 알 수 없는 대상은 파괴 대상이 아니다.
        - 이름이 접미사와 정확히 같은 경우(`_test`): 허용된다. 규칙을 지킨
          이름이며, 그런 DB 를 만드는 것 자체가 명시적 행위다.
    """
    if not database.endswith(DISPOSABLE_DATABASE_SUFFIX):
        raise ProductionDatabaseError(
            f"파괴적 작업이 '{database or '(알 수 없음)'}' 데이터베이스를 대상으로 했다. "
            f"'{DISPOSABLE_DATABASE_SUFFIX}' 로 끝나는 DB 에서만 허용된다 — 즉시 중단한다. "
            "테스트가 운영 DB 를 비운 사건이 실제로 있었고 운영 실행 기록이 소실됐다 "
            "(docs/incidents/test-truncated-operations-history.md)"
        )
    return database


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
