"""시점 질의 3종 — 특히 정정이 있었을 때 감사 재현이 그때 행을 돌려주는지.

이 테스트가 존재하는 이유: 질의 3(감사 재현)이 이 시스템의 핵심 가치다.
F-3(소명 불가)을 막는 것이 그 값이며, 그 값은 **네 개의 시간 조건이 모두 걸릴 때만**
나온다. `known_from <= t < known_until` 을 빠뜨려도 질의는 정상 동작하고 결과도
그럴듯하다 — 그때 볼 수 없었던 조문이 섞일 뿐이다. 눈으로는 잡히지 않는다.

`valid_from` 이 실제 값으로 채워지려면 일자별 이력 API 결합이 필요하고 그것은
다음 작업이다. 그래서 여기서는 픽스처를 직접 INSERT 한다 — 실제 데이터가 없다고
질의를 검증하지 않으면, 나중에 결합할 때 질의가 맞는지 알 수 없다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from regchange.store.queries import (
    AS_KNOWN_AT_SQL,
    as_known_at,
    count_pending_valid_from,
    currently_effective,
    explain,
    pending_effective,
)

pytestmark = pytest.mark.requires_db

# 시간축 두 개를 눈으로 구별할 수 있게 값을 떨어뜨려 놓는다.
KNOWN_FIRST = dt.datetime(2026, 2, 1, 0, 0, tzinfo=dt.UTC)
"""처음 수집한 시각 — 담당자가 2026-03-15에 보던 정보는 이 시점의 것이다."""

KNOWN_CORRECTION = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.UTC)
"""정정을 알게 된 시각. 2026-03-15 화면에는 나타나면 안 된다."""

AUDIT_DATE = dt.date(2026, 3, 15)
AUDIT_INSTANT = dt.datetime(2026, 3, 15, 23, 59, 59, tzinfo=dt.UTC)
TODAY = dt.date(2026, 8, 17)


async def _insert_document(conn: psycopg.AsyncConnection[Any], mst: str) -> UUID:
    document_id = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_document (
                id, law_id, mst, law_name, document_effective_date,
                source_key, source_run_id, source_page_sha256, load_run_id, known_from
            ) VALUES (%s, '009244', %s, '특금법', DATE '2024-01-01',
                      'k', 'r', %s, %s, %s)
            """,
            (document_id, mst, "0" * 64, uuid4(), KNOWN_FIRST),
        )
    await conn.commit()
    return document_id


async def _insert_article(
    conn: psycopg.AsyncConnection[Any],
    document_id: UUID,
    *,
    seq: int,
    text: str,
    valid_from: dt.date,
    valid_to: dt.date = dt.date(9999, 12, 31),
    known_from: dt.datetime = KNOWN_FIRST,
) -> UUID:
    """`valid_from` 이 채워진 조문. 출처는 HISTORY_API 다 — 본문에서 온 값이 아니다."""
    article_id = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_article (
                id, document_id, article_key, seq_in_doc, unit_type,
                article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                body_norm, body_norm_sha256,
                norm_rule_version, valid_from, valid_to, valid_from_source,
                known_from, load_run_id
            ) VALUES (%s, %s, %s, %s, 'ARTICLE', %s, 0, %s, %s, %s,
                      %s, %s,
                      'norm-v2', %s, %s, 'HISTORY_API', %s, %s)
            """,
            (
                article_id,
                document_id,
                f"{seq + 1:04d}001",
                seq,
                seq + 1,
                text,
                text,
                f"{seq:064d}",
                text,
                f"{seq:064d}",
                valid_from,
                valid_to,
                known_from,
                uuid4(),
            ),
        )
    await conn.commit()
    return article_id


async def _close(conn: psycopg.AsyncConnection[Any], article_id: UUID) -> None:
    """정정: 기존 행을 닫는다. UPDATE 로 내용을 고치지 않는다 (원칙 6)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE regulation_article SET known_until = %s WHERE id = %s",
            (KNOWN_CORRECTION, article_id),
        )
    await conn.commit()


async def test_currently_effective_returns_only_open_rows_in_force(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """질의 1 — 지금 시행 중이고 지금 아는 것."""
    document_id = await _insert_document(owner_conn, "252787")
    await _insert_article(
        owner_conn, document_id, seq=0, text="시행 중", valid_from=dt.date(2024, 1, 1)
    )
    await _insert_article(
        owner_conn, document_id, seq=1, text="시행 예정", valid_from=dt.date(2027, 1, 1)
    )
    await _insert_article(
        owner_conn,
        document_id,
        seq=2,
        text="이미 종료",
        valid_from=dt.date(2020, 1, 1),
        valid_to=dt.date(2021, 1, 1),
    )

    rows = await currently_effective(owner_conn, as_of=TODAY)
    assert [row["text_raw"] for row in rows] == ["시행 중"]


async def test_pending_effective_is_ordered_as_a_dday_queue(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """질의 2 — 시행 예정이 시행일 순으로 나온다."""
    document_id = await _insert_document(owner_conn, "252787")
    await _insert_article(
        owner_conn, document_id, seq=0, text="나중", valid_from=dt.date(2027, 6, 1)
    )
    await _insert_article(
        owner_conn, document_id, seq=1, text="먼저", valid_from=dt.date(2026, 12, 1)
    )
    await _insert_article(
        owner_conn, document_id, seq=2, text="이미 시행", valid_from=dt.date(2024, 1, 1)
    )

    rows = await pending_effective(owner_conn, as_of=TODAY)
    assert [row["text_raw"] for row in rows] == ["먼저", "나중"]


async def test_audit_replay_returns_the_row_that_was_open_at_that_moment(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """질의 3 — 정정이 있었던 조문의 과거 시점 조회가 **닫힌 행**을 돌려준다.

    이것이 이 파일의 핵심이다. 2026-03-15에 담당자가 본 것은 정정 전 원문이며,
    2026-05-01에 알게 된 정정본이 그 화면에 있으면 재현이 아니다.
    """
    document_id = await _insert_document(owner_conn, "252787")
    original = await _insert_article(
        owner_conn, document_id, seq=0, text="정정 전 원문", valid_from=dt.date(2024, 1, 1)
    )
    await _close(owner_conn, original)
    await _insert_article(
        owner_conn,
        document_id,
        seq=0,
        text="정정 후 원문",
        valid_from=dt.date(2024, 1, 1),
        known_from=KNOWN_CORRECTION,
    )

    past = await as_known_at(owner_conn, valid_at=AUDIT_DATE, known_at=AUDIT_INSTANT)
    assert [row["text_raw"] for row in past] == ["정정 전 원문"]
    assert past[0]["known_until"] == KNOWN_CORRECTION

    # 같은 조문을 지금 시점에서 보면 정정본이 나온다. 둘 다 남아 있다.
    now = await currently_effective(owner_conn, as_of=TODAY)
    assert [row["text_raw"] for row in now] == ["정정 후 원문"]


async def test_audit_replay_hides_rows_collected_after_that_moment(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """그 시점에 아직 수집하지 않은 조문은 결과에 없다. 없는 것이 정답이다."""
    document_id = await _insert_document(owner_conn, "252787")
    await _insert_article(
        owner_conn, document_id, seq=0, text="그때 알던 것", valid_from=dt.date(2024, 1, 1)
    )
    await _insert_article(
        owner_conn,
        document_id,
        seq=1,
        text="나중에 알게 된 것",
        valid_from=dt.date(2024, 1, 1),
        known_from=KNOWN_CORRECTION,
    )

    past = await as_known_at(owner_conn, valid_at=AUDIT_DATE, known_at=AUDIT_INSTANT)
    assert [row["text_raw"] for row in past] == ["그때 알던 것"]


async def test_pending_valid_from_rows_are_invisible_to_all_three_queries(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """`valid_from` 이 없는 조문은 세 질의 어디에도 잡히지 않는다.

    의도된 동작이다 — 시행일을 모르는 조문을 "오늘 시행 중"이라고 답할 수 없다.
    그대로 두면 "결과 0건"과 "데이터 없음"이 구별되지 않으므로, 별도 집계가
    그 수를 드러낸다.
    """
    document_id = await _insert_document(owner_conn, "252787")
    async with owner_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_article (
                id, document_id, article_key, seq_in_doc, unit_type,
                article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                body_norm, body_norm_sha256,
                norm_rule_version, valid_from_source, known_from, load_run_id
            ) VALUES (%s, %s, '0001001', 0, 'ARTICLE', 1, 0, '미결합', '미결합', %s,
                      '미결합', %s,
                      'norm-v2', 'PENDING_HISTORY', %s, %s)
            """,
            (uuid4(), document_id, "9" * 64, "9" * 64, KNOWN_FIRST, uuid4()),
        )
    await owner_conn.commit()

    assert await currently_effective(owner_conn, as_of=TODAY) == []
    assert await pending_effective(owner_conn, as_of=TODAY) == []
    assert await as_known_at(owner_conn, valid_at=AUDIT_DATE, known_at=AUDIT_INSTANT) == []
    assert await count_pending_valid_from(owner_conn) == 1


async def test_headings_are_never_returned_as_citable_articles(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """편장절관 제목행은 인용 대상이 아니다 (ADR-001)."""
    document_id = await _insert_document(owner_conn, "252787")
    async with owner_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO regulation_article (
                id, document_id, article_key, seq_in_doc, unit_type,
                article_no, branch_no, text_raw, text_norm, text_norm_sha256,
                body_norm, body_norm_sha256,
                norm_rule_version, valid_from, valid_from_source, known_from, load_run_id
            ) VALUES (%s, %s, '0011000', 0, 'HEADING', 1, 0, %s, %s, %s,
                      %s, %s,
                      'norm-v2', DATE '2024-01-01', 'HISTORY_API', %s, %s)
            """,
            (
                uuid4(),
                document_id,
                "제2편 금융투자업",
                "제2편 금융투자업",
                "8" * 64,
                "제2편 금융투자업",
                "8" * 64,
                KNOWN_FIRST,
                uuid4(),
            ),
        )
    await owner_conn.commit()

    assert await currently_effective(owner_conn, as_of=TODAY) == []


async def test_audit_replay_uses_the_bitemporal_index(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """질의 3이 bitemporal 인덱스를 탄다 (완료 조건 10).

    작은 테이블에서는 플래너가 순차 스캔을 고르므로, 인덱스가 **존재하고 계획에
    고려되는지**를 확인한다. 인덱스 이름을 단언하면 리팩터링 때 깨지므로,
    강제한 상태에서 계획이 성립하는지를 본다.
    """
    document_id = await _insert_document(owner_conn, "252787")
    await _insert_article(
        owner_conn, document_id, seq=0, text="본문", valid_from=dt.date(2024, 1, 1)
    )

    async with owner_conn.cursor() as cur:
        await cur.execute("SET LOCAL enable_seqscan = off")
        plan = await explain(
            owner_conn, AS_KNOWN_AT_SQL, {"valid_at": AUDIT_DATE, "known_at": AUDIT_INSTANT}
        )
    await owner_conn.rollback()

    assert "regulation_article_bitemporal" in plan, plan
