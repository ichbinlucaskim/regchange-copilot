"""실행 이력 조회 — 운영 실적 숫자가 맞는지.

이 테스트가 존재하는 이유: `ops summary` 의 출력이 README 와 이력서에 그대로 들어간다.
"N일간 운영, 실패 K일"이 틀리면 그것은 오타가 아니라 **틀린 주장**이다. 특히 두 가지를
고정한다.

  1. KST 달력으로 센다 — 07:00 KST 실행은 UTC 로 전날이며, UTC 로 세면 하루 밀린다
  2. 하루에 두 번 돌면 그날의 **가장 나쁜 상태**가 그 날의 상태다 (성공이 실패를 덮지 않는다)
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
import pytest

from regchange.ingest.canary import CanaryResult, IngestRunStatus
from regchange.ops import fetch_alerts, fetch_history, fetch_summary, record_run
from regchange.ops.history import AlertKind, consecutive_zero
from regchange.ops.models import (
    DailyRunResult,
    DateProbe,
    DetectedLaw,
    LawOutcome,
    LawOutcomeStatus,
    OpsRunStatus,
)

pytestmark = pytest.mark.requires_db

NOW = dt.datetime(2026, 8, 20, 3, 0, tzinfo=dt.UTC)
"""2026-08-20 12:00 KST."""

PASSED = CanaryResult(passed=True, total_count=83, detail="통과")
FAILED_CANARY = CanaryResult(passed=False, total_count=None, detail="카나리아 실패")


def _result(
    *,
    started_at: dt.datetime,
    status: OpsRunStatus,
    seed: str,
    outcomes: tuple[LawOutcome, ...] = (),
) -> DailyRunResult:
    """이력 조회용 실행 결과 하나를 만든다. 상태를 직접 지정한다."""
    canary = PASSED if status is not OpsRunStatus.SKIPPED_CANARY_FAILED else FAILED_CANARY
    return DailyRunResult(
        run_id=f"{started_at:%Y%m%dT%H%M%SZ}-{seed}",
        started_at=started_at,
        finished_at=started_at + dt.timedelta(seconds=30),
        status=status,
        lookback_days=7,
        target_dates=("20260819",),
        canary=canary,
        probes=(
            DateProbe(
                reg_date="20260819",
                status=IngestRunStatus.SUCCEEDED,
                total_count=12,
                matched=len(outcomes),
                detail="",
            ),
        ),
        outcomes=outcomes,
        requests=8,
        retries=0,
        detail=f"테스트 실행 ({status.value})",
    )


def _failed_law(detail: str) -> LawOutcome:
    return LawOutcome(
        detected=DetectedLaw(reg_date="20260819", law_id="009244", law_name="특금법", mst="215971"),
        status=LawOutcomeStatus.FAILED,
        failure_detail=detail,
    )


async def test_history_shows_failures_with_their_reason(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """실패를 본 사람이 사유를 보러 한 번 더 움직이지 않아도 되게 한다."""
    await record_run(
        owner_conn,
        _result(
            started_at=NOW - dt.timedelta(days=1),
            status=OpsRunStatus.PARTIAL,
            seed="aaaa",
            outcomes=(_failed_law("TransportError: 연결 실패"),),
        ),
    )

    rows = await fetch_history(owner_conn, days=30, now=NOW)

    assert len(rows) == 1
    assert rows[0].status is OpsRunStatus.PARTIAL
    assert rows[0].laws_failed == 1
    assert "TransportError" in rows[0].failures[0]


async def test_run_date_uses_the_kst_calendar(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """07:00 KST 실행은 UTC 로 전날 22:00 이다. UTC 로 세면 날짜가 하루 밀린다."""
    await record_run(
        owner_conn,
        _result(
            started_at=dt.datetime(2026, 8, 18, 22, 0, tzinfo=dt.UTC),
            status=OpsRunStatus.SUCCEEDED_ZERO,
            seed="bbbb",
        ),
    )

    rows = await fetch_history(owner_conn, days=30, now=NOW)

    assert rows[0].run_date == dt.date(2026, 8, 19)


async def test_summary_counts_missing_days_from_the_calendar(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """**미실행일은 기록의 부재다.** 부재는 기록할 수 없으므로 달력에서 뺀다."""
    for offset, status in ((5, OpsRunStatus.SUCCEEDED), (1, OpsRunStatus.SUCCEEDED_ZERO)):
        await record_run(
            owner_conn,
            _result(
                started_at=NOW - dt.timedelta(days=offset),
                status=status,
                seed=f"c{offset:03d}",
            ),
        )

    summary = await fetch_summary(owner_conn, now=NOW)

    assert summary is not None
    assert summary.first_run_date == dt.date(2026, 8, 15)
    assert summary.calendar_days == 6  # 8/15 ~ 8/20
    assert summary.executed_days == 2
    assert summary.missing_days == 4
    assert summary.succeeded_days == 1
    assert summary.zero_days == 1


async def test_two_runs_in_a_day_fold_to_the_worst_status(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """성공한 재실행이 실패를 덮으면 실패가 사라진다."""
    day = NOW - dt.timedelta(days=1)
    await record_run(owner_conn, _result(started_at=day, status=OpsRunStatus.FAILED, seed="d001"))
    await record_run(
        owner_conn,
        _result(started_at=day + dt.timedelta(hours=1), status=OpsRunStatus.SUCCEEDED, seed="d002"),
    )

    summary = await fetch_summary(owner_conn, now=NOW)

    assert summary is not None
    assert summary.executed_days == 1
    assert summary.total_runs == 2
    assert summary.failed_days == 1
    assert summary.succeeded_days == 0


async def test_summary_is_none_when_nothing_has_run(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """0일 운영이라는 값을 지어내지 않는다."""
    assert await fetch_summary(owner_conn, now=NOW) is None


async def test_canary_failure_becomes_an_alert(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """미수행은 실패율에 넣지 않지만 **사람은 봐야 한다.**"""
    await record_run(
        owner_conn,
        _result(
            started_at=NOW - dt.timedelta(hours=5),
            status=OpsRunStatus.SKIPPED_CANARY_FAILED,
            seed="e001",
        ),
    )

    alerts = await fetch_alerts(owner_conn, days=7, now=NOW)

    assert [alert.kind for alert in alerts] == [AlertKind.CANARY_FAILED]


async def test_a_short_zero_streak_is_not_an_alert(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """0건은 기본 상태다 — 코퍼스 12개월 실측이 개정일 14일/365일이다."""
    for offset in (3, 2, 1):
        await record_run(
            owner_conn,
            _result(
                started_at=NOW - dt.timedelta(days=offset),
                status=OpsRunStatus.SUCCEEDED_ZERO,
                seed=f"f{offset:03d}",
            ),
        )

    streak, span = await consecutive_zero(owner_conn, now=NOW)

    assert streak == 3
    assert span == 3
    assert await fetch_alerts(owner_conn, days=7, now=NOW) == ()


async def test_a_long_zero_streak_is_an_alert(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """**실행 수가 아니라 달력 구간으로 판정한다.** 두 번 돌았는데 그 사이가 석 달일 수 있다."""
    for offset in (100, 1):
        await record_run(
            owner_conn,
            _result(
                started_at=NOW - dt.timedelta(days=offset),
                status=OpsRunStatus.SUCCEEDED_ZERO,
                seed=f"g{offset:03d}",
            ),
        )

    alerts = await fetch_alerts(owner_conn, days=7, now=NOW)

    assert [alert.kind for alert in alerts] == [AlertKind.CONSECUTIVE_ZERO]
    assert "100일" in alerts[0].subject


async def test_a_non_zero_run_resets_the_streak(
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """가장 최근 실행이 0건이 아니면 연속은 끊긴 것이다."""
    await record_run(
        owner_conn,
        _result(
            started_at=NOW - dt.timedelta(days=100),
            status=OpsRunStatus.SUCCEEDED_ZERO,
            seed="h001",
        ),
    )
    await record_run(
        owner_conn,
        _result(started_at=NOW - dt.timedelta(days=1), status=OpsRunStatus.SUCCEEDED, seed="h002"),
    )

    assert await consecutive_zero(owner_conn, now=NOW) == (0, 0)
