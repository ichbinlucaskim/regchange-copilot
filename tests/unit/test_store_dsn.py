"""파괴적 작업의 대상 DB 가드.

이 테스트가 존재하는 이유: **실제로 일어난 사고를 막는 장치**다. 테스트의 `TRUNCATE`
가 운영 DB 를 비워 첫 운영 실행 기록이 소실됐다
(`docs/incidents/test-truncated-operations-history.md`).

DB 를 분리한 것만으로는 부족하다 — 분리는 설정이고 설정은 `.env` 한 줄로 되돌아온다.
이 가드가 그 설정 위에 놓이는 구조이며, **장치를 만들고 발화시켜 보지 않으면 그것이
작동하는지 모른다**(R-21 탐지 장치와 같은 규칙).
"""

from __future__ import annotations

import pytest

from regchange.store.dsn import (
    DEFAULT_OWNER_DSN,
    DISPOSABLE_DATABASE_SUFFIX,
    LOCAL_DATABASE,
    ProductionDatabaseError,
    assert_disposable_database,
    database_of,
)


def test_production_database_is_rejected() -> None:
    """**가드 발화.** 운영 DB 이름이면 즉시 예외다. 경고가 아니다."""
    with pytest.raises(ProductionDatabaseError, match=LOCAL_DATABASE):
        assert_disposable_database(LOCAL_DATABASE)


def test_the_actual_production_name_does_not_pass_by_accident() -> None:
    """위 테스트가 다른 이유로 통과하는 것을 막는다.

    운영 DB 이름이 우연히 `_test` 로 끝나면 가드는 통과하고 테스트도 통과한다 —
    그때 이 프로젝트의 방어는 사라진 채로 초록불이 켜진다.
    """
    assert not LOCAL_DATABASE.endswith(DISPOSABLE_DATABASE_SUFFIX)


def test_test_database_is_allowed() -> None:
    """규칙을 지킨 이름은 통과한다. 가드가 전부를 막으면 쓸모가 없다."""
    assert assert_disposable_database("regchange_test") == "regchange_test"


def test_other_disposable_names_are_allowed() -> None:
    """접미사 규칙이라 CI·PR 별 DB 가 목록 수정 없이 허용된다."""
    assert assert_disposable_database("regchange_pr123_test") == "regchange_pr123_test"


def test_unknown_database_is_rejected() -> None:
    """이름을 모르면 파괴 대상이 아니다. 빈 값을 관대하게 다루지 않는다."""
    with pytest.raises(ProductionDatabaseError, match="알 수 없음"):
        assert_disposable_database("")


def test_database_of_reads_the_name_from_a_dsn() -> None:
    assert database_of(DEFAULT_OWNER_DSN) == LOCAL_DATABASE


def test_database_of_ignores_query_parameters() -> None:
    """`?sslmode=require` 가 붙은 관리형 DSN 에서 이름이 오염되면 가드가 오작동한다."""
    dsn = "postgresql://u:p@host:5432/regchange_test?sslmode=require"
    assert database_of(dsn) == "regchange_test"


def test_dsn_without_a_database_yields_the_unknown_name() -> None:
    """경로가 없으면 빈 문자열이고, 그 값은 가드에서 거부된다."""
    assert database_of("postgresql://u:p@host:5432") == ""
    with pytest.raises(ProductionDatabaseError):
        assert_disposable_database(database_of("postgresql://u:p@host:5432"))
