"""bitemporal 스키마가 실제로 과거를 지키는지 DB 에 물어서 확인한다.

이 테스트가 존재하는 이유: 원칙 6("UPDATE 로 과거를 지우지 않는다")은 규율이
아니라 제약이어야 한다. 규율은 급한 수정 한 번으로 무너지고, 무너진 뒤에는
무엇이 지워졌는지 알 방법이 없다. 그래서 트리거가 실제로 막는지를 코드가 아니라
DB 에 물어 확인한다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

pytestmark = pytest.mark.requires_db

NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.UTC)

RC_TEXT_IMMUTABLE = "RC001"
RC_UPDATE_FORBIDDEN = "RC002"
RC_DELETE_FORBIDDEN = "RC003"
"""커스텀 SQLSTATE 는 psycopg 의 예외 계층에 매핑되지 않아 `DatabaseError` 로 올라온다.
메시지가 아니라 `sqlstate` 로 단언하는 이유: 문구를 다듬는 순간 메시지 단언은 조용히
통과한다."""


async def _insert_document(
    conn: psycopg.AsyncConnection[Any],
    *,
    mst: str = "252787",
    known_from: dt.datetime = NOW,
) -> UUID:
    document_id = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_document (
                id, law_id, mst, law_name, document_effective_date,
                source_key, source_run_id, source_page_sha256, load_run_id, known_from
            ) VALUES (%s, '009244', %s, '특정 금융거래정보의 보고 및 이용 등에 관한 법률',
                      DATE '2023-07-18', 'k', 'r', %s, %s, %s)
            """,
            (document_id, mst, "0" * 64, uuid4(), known_from),
        )
    await conn.commit()
    return document_id


async def _insert_article(
    conn: psycopg.AsyncConnection[Any],
    document_id: UUID,
    *,
    seq: int = 0,
    text: str = "원문",
    known_from: dt.datetime = NOW,
) -> UUID:
    article_id = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_article (
                id, document_id, article_key, seq_in_doc, unit_type,
                article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                norm_rule_version, valid_from_source, known_from, load_run_id
            ) VALUES (%s, %s, '0001001', %s, 'ARTICLE', 1, 0, %s, %s, %s,
                      'norm-v2', 'PENDING_HISTORY', %s, %s)
            """,
            (article_id, document_id, seq, text, text, "1" * 64, known_from, uuid4()),
        )
    await conn.commit()
    return article_id


async def test_text_update_is_blocked_by_trigger(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """`text_raw` 를 UPDATE 하면 전용 오류 코드로 거부된다 (완료 조건 2)."""
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    with pytest.raises(psycopg.DatabaseError) as caught:
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE regulation_article SET text_raw = '조작' WHERE id = %s", (article_id,)
            )
    assert caught.value.sqlstate == RC_TEXT_IMMUTABLE
    await owner_conn.rollback()


async def test_text_norm_update_is_blocked(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """정규화본도 같은 경로로 막힌다. 원문만 막으면 비교 대상이 조작된다."""
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    with pytest.raises(psycopg.DatabaseError) as caught:
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE regulation_article SET text_norm = '조작' WHERE id = %s", (article_id,)
            )
    assert caught.value.sqlstate == RC_TEXT_IMMUTABLE
    await owner_conn.rollback()


async def test_arbitrary_column_update_is_blocked(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """텍스트가 아닌 컬럼도 막힌다.

    트리거가 컬럼 이름을 나열하지 않으므로, 나중에 컬럼이 늘어도 함께 고칠 것이 없다.
    """
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    with pytest.raises(psycopg.DatabaseError) as caught:
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE regulation_article SET title = '조작' WHERE id = %s", (article_id,)
            )
    assert caught.value.sqlstate == RC_UPDATE_FORBIDDEN
    await owner_conn.rollback()


async def test_delete_is_blocked(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """DELETE 는 어떤 경우에도 막힌다."""
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    with pytest.raises(psycopg.DatabaseError) as caught:
        async with owner_conn.cursor() as cur:
            await cur.execute("DELETE FROM regulation_article WHERE id = %s", (article_id,))
    assert caught.value.sqlstate == RC_DELETE_FORBIDDEN
    await owner_conn.rollback()


async def test_closing_known_until_is_the_only_allowed_update(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """정정 경로는 열려 있어야 한다 — 닫고 새 행을 넣는 것이 유일한 방법이다."""
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "UPDATE regulation_article SET known_until = %s WHERE id = %s", (LATER, article_id)
        )
    await owner_conn.commit()

    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT known_until FROM regulation_article WHERE id = %s", (article_id,))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == LATER


async def test_closed_row_cannot_be_reopened(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """한 번 닫힌 행은 다시 손댈 수 없다. 닫힌 과거를 되살리는 경로를 남기지 않는다."""
    document_id = await _insert_document(owner_conn)
    article_id = await _insert_article(owner_conn, document_id)

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "UPDATE regulation_article SET known_until = %s WHERE id = %s", (LATER, article_id)
        )
    await owner_conn.commit()

    with pytest.raises(psycopg.DatabaseError) as caught:
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE regulation_article SET known_until = 'infinity' WHERE id = %s",
                (article_id,),
            )
    assert caught.value.sqlstate == RC_UPDATE_FORBIDDEN
    await owner_conn.rollback()


async def test_two_open_rows_with_same_natural_key_are_rejected(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """열린 행끼리는 자연키가 유일해야 한다 (ADR-001)."""
    document_id = await _insert_document(owner_conn)
    await _insert_article(owner_conn, document_id, seq=0)

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insert_article(owner_conn, document_id, seq=0, text="다른 원문")
    await owner_conn.rollback()


async def test_closed_row_frees_the_natural_key(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """닫힌 행은 유니크 제약에서 빠진다 — 그것이 정정 이력이 쌓이는 방식이다."""
    document_id = await _insert_document(owner_conn)
    first = await _insert_article(owner_conn, document_id, seq=0, text="원문 v1")

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "UPDATE regulation_article SET known_until = %s WHERE id = %s", (LATER, first)
        )
    await owner_conn.commit()

    second = await _insert_article(owner_conn, document_id, seq=0, text="원문 v2", known_from=LATER)
    assert second != first

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM regulation_article WHERE document_id = %s", (document_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 2, "닫힌 행과 열린 행이 함께 남는다"


async def test_pending_history_cannot_carry_a_valid_from(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """출처가 PENDING_HISTORY 인데 값이 있으면 스키마가 거부한다.

    이 CHECK 가 막는 것: 어딘가에서 문서 시행일을 `valid_from` 으로 승격하는 일.
    본문 API 의 조문시행일자는 문서 시행일로 평탄화되어 있으므로(edge-case #8),
    그 값이 `valid_from` 이 되면 원칙 6이 조용히 무너진다.
    """
    document_id = await _insert_document(owner_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO regulation_article (
                    id, document_id, article_key, seq_in_doc, unit_type,
                    article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                    norm_rule_version, valid_from, valid_from_source, known_from, load_run_id
                ) VALUES (%s, %s, '0001001', 0, 'ARTICLE', 1, 0, 'x', 'x', %s,
                          'norm-v2', DATE '2023-07-18', 'PENDING_HISTORY', %s, %s)
                """,
                (uuid4(), document_id, "1" * 64, NOW, uuid4()),
            )
    await owner_conn.rollback()


async def test_history_api_source_requires_a_valid_from(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """반대 방향도 막는다 — 출처가 HISTORY_API 인데 값이 없을 수 없다."""
    document_id = await _insert_document(owner_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO regulation_article (
                    id, document_id, article_key, seq_in_doc, unit_type,
                    article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                    norm_rule_version, valid_from_source, known_from, load_run_id
                ) VALUES (%s, %s, '0001001', 0, 'ARTICLE', 1, 0, 'x', 'x', %s,
                          'norm-v2', 'HISTORY_API', %s, %s)
                """,
                (uuid4(), document_id, "1" * 64, NOW, uuid4()),
            )
    await owner_conn.rollback()


async def test_ministry_master_rejects_boundary_row_with_org_code(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """경계 관측에 org_code 를 붙이려는 시도를 스키마가 막는다.

    코드를 붙이는 것은 조직 동일성 판단이고, 그 판단은 사람이 한다 (ADR-009).
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ministry_master (
                    id, org_code, org_name, source_field, source, valid_from, known_from
                ) VALUES (%s, '1482000', '환경부령', '법령구분명', 'OBSERVED_BOUNDARY',
                          DATE '2025-08-07', %s)
                """,
                (uuid4(), NOW),
            )
    await owner_conn.rollback()


async def test_load_run_rejects_counts_that_do_not_partition(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """건수 단언을 DB 에도 건다. 애플리케이션 검사만으로는 우회가 남는다."""
    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO load_run (
                    id, source_key, source_run_id, started_at, completed_at,
                    documents_loaded, parsed_units, loaded, loaded_unresolved,
                    skipped, key_conflicts
                ) VALUES (%s, 'k', 'r', %s, %s, 1, 10, 3, 0, 0, 0)
                """,
                (uuid4(), NOW, NOW),
            )
    await owner_conn.rollback()


async def test_ministry_master_seed_contains_both_sources(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """시드가 두 출처를 모두 담고, 경계일 2개가 반영되어 있다 (완료 조건 5)."""
    async with owner_conn.cursor() as cur:
        await cur.execute(
            "SELECT source, count(*) FROM ministry_master GROUP BY source ORDER BY source"
        )
        by_source = dict(await cur.fetchall())
        await cur.execute(
            """
            SELECT DISTINCT valid_from FROM ministry_master
             WHERE source = 'OBSERVED_BOUNDARY' AND valid_from >= DATE '2025-10-01'
             ORDER BY valid_from
            """
        )
        boundaries = [row[0] for row in await cur.fetchall()]

    assert by_source["OBSERVED_FLATTENED"] > 0
    assert by_source["OBSERVED_BOUNDARY"] == 8
    assert dt.date(2025, 10, 1) in boundaries
    assert dt.date(2026, 1, 2) in boundaries, "개편이 한 번에 일어나지 않는다 (ADR-009)"


async def test_boundary_rows_are_not_closed(owner_conn: psycopg.AsyncConnection[Any]) -> None:
    """경계 행의 `valid_until` 은 전부 NULL 이다.

    마지막 관측일은 종료일이 아니다. 마지막 관측일 다음 날로 닫으면 관측하지 않은
    종료를 주장하게 된다.
    """
    async with owner_conn.cursor() as cur:
        await cur.execute(
            """
            SELECT count(*) FROM ministry_master
             WHERE source = 'OBSERVED_BOUNDARY' AND valid_until IS NOT NULL
            """
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0
