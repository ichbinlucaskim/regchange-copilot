"""0.5단계 읽기 전용 role 테스트 — 무엇을 증명하지 못했는지의 기록.

## 무엇이 잘못됐었나

0.5단계의 이 파일은 `regchange_llm_ro` 로 `CREATE TABLE` 과 `CREATE SCHEMA` 가
거부되는지 검사하고, 그것을 "LLM 경로가 쓰기를 하지 못한다"의 근거로 삼았다.

**그 검사는 원칙 5를 증명하지 못한다.** Postgres 15+ 는 PUBLIC 의 public 스키마
CREATE 권한을 기본으로 회수하므로, **INSERT·UPDATE·DELETE 를 전부 가진 role 도
`CREATE TABLE` 에서 거부된다.** 그 테스트가 실제로 증명한 것은 "이 role 이 스키마
소유자가 아니다"이며, 읽기 전용인지와는 무관하다.

아래 `test_the_old_assertion_passes_even_for_a_writing_role` 이 그 사실을 실증한다 —
규제 테이블에 INSERT 할 수 있는 `app_ingest` 도 옛 단언을 그대로 통과한다.

## 왜 그때는 드러나지 않았나

**검사할 대상이 없었기 때문이다.** 0.5단계에는 테이블이 하나도 없었고, 그래서
"쓰기가 거부되는가"를 물을 대상이 없었다. 물을 수 없는 것을 물었다고 착각한
자리에 통과하는 테스트가 남았고, 통과하는 테스트는 **경계가 있다는 증거처럼
읽힌다.** 이것은 이 저장소가 반복해서 겪은 형태다 — 검사하지 않은 것과 검사해서
통과한 것이 같은 초록색으로 보인다.

## 지금은 어디서 검사하나

`tests/security/test_db_roles.py` 가 role 4종으로 실제 테이블에 대해 INSERT /
UPDATE / DELETE / SELECT / TRUNCATE 를 시도하고 거부를 확인한다.

## 이 파일을 지우지 않는 이유

지우면 "그런 착각이 있었다"는 사실이 사라진다. 같은 형태의 테스트가 다시 쓰일 때
막을 근거가 없어진다. `regchange_llm_ro` 자체는 app_graph 로 대체됐으나, 기존
접속 문자열을 깨지 않기 위해 남겨 두었고 새 권한을 주지 않았다.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from regchange.store.dsn import CONNECT_TIMEOUT_SECONDS, DbRole, role_dsn

pytestmark = [pytest.mark.security, pytest.mark.requires_db]

DEFAULT_READONLY_URL = (
    "postgresql://regchange_llm_ro:regchange_ro_local_dev_only@localhost:5433/regchange"
)


def _connect(url: str) -> psycopg.Connection[Any]:
    try:
        return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres 에 접속할 수 없다 (make up 필요): {exc}")


def test_pgvector_extension_is_installed() -> None:
    """pgvector 확장이 설치되어 있어야 retrieval 이 성립한다."""
    url = os.environ.get("DATABASE_URL_READONLY", DEFAULT_READONLY_URL)
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()

    assert row is not None, "pgvector 확장이 설치되어 있지 않다"


def test_the_old_assertion_passes_even_for_a_writing_role() -> None:
    """옛 단언이 무엇을 증명하지 못했는지 실증한다.

    이 테스트가 막는 위협: "CREATE 가 거부되므로 읽기 전용이다"라는 추론이 다시
    쓰이는 것. `app_ingest` 는 규제 테이블에 INSERT 할 수 있는데도 아래 두 단언을
    그대로 통과한다. 통과한다는 사실 자체가 그 단언이 비어 있다는 증거다.

    이 테스트가 **실패한다면** Postgres 의 기본 권한 모델이 바뀐 것이므로,
    `test_db_roles.py` 의 전제도 함께 다시 확인해야 한다.
    """
    with _connect(role_dsn(DbRole.INGEST)) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("CREATE TABLE security_probe_should_fail (id int)")
        conn.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("CREATE SCHEMA security_probe_should_fail")
        conn.rollback()

        # 그런데 이 role 은 쓰기를 할 수 있다. 옛 단언은 이 사실과 양립한다.
        cur.execute("SELECT has_table_privilege('regulation_document', 'INSERT')")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True, "쓰기 권한이 있는 role 인데도 위 단언을 통과했다"
        conn.rollback()
