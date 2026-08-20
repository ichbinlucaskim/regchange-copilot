"""폴링 창 계산과 실행 상태 판정 — 운영 실적 숫자가 여기서 나온다.

이 테스트가 존재하는 이유: "며칠 성공했나"는 `derive_status` 가 정하고, "어느 날짜를
보나"는 `lookback_dates` 가 정한다. 둘 다 경계 조건이 본체다 — 오늘을 포함하면 등재
지연이 0건으로 위장하고, 부분 실패를 성공으로 접으면 실패가 사라진다. 그 경계를
DB 없이 고정한다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from regchange.ingest.canary import IngestRunStatus
from regchange.ops.models import (
    DateProbe,
    DetectedLaw,
    LawOutcome,
    LawOutcomeStatus,
    OpsRunStatus,
    derive_status,
    lookback_dates,
)

KST_MORNING = dt.datetime(2026, 8, 19, 22, 0, tzinfo=dt.UTC)
"""2026-08-20 07:00 KST. cron 이 실제로 도는 시각이며 UTC 로는 전날이다."""


def _probe(status: IngestRunStatus) -> DateProbe:
    return DateProbe(reg_date="20260819", status=status, total_count=1, matched=0, detail="")


def _outcome(status: LawOutcomeStatus) -> LawOutcome:
    detected = DetectedLaw(reg_date="20260819", law_id="009244", law_name=None, mst="215971")
    return LawOutcome(
        detected=detected,
        status=status,
        failure_detail="실패" if status is LawOutcomeStatus.FAILED else None,
    )


# ---------------------------------------------------------------------------
# 폴링 창
# ---------------------------------------------------------------------------


def test_window_uses_the_kst_calendar_not_utc() -> None:
    """07:00 KST 실행은 UTC 로 전날이다. UTC 로 세면 창이 하루 밀린다."""
    assert lookback_dates(KST_MORNING, 1) == ("20260819",)


def test_today_is_never_polled() -> None:
    """당일 폴링은 법제처 등재 전일 수 있고, 그 0은 '개정 없음'으로 흘러간다."""
    assert "20260820" not in lookback_dates(KST_MORNING, 7)


def test_window_is_oldest_first_and_covers_n_days() -> None:
    """오래된 순서여야 로그가 시간순으로 읽힌다."""
    assert lookback_dates(KST_MORNING, 7) == (
        "20260813",
        "20260814",
        "20260815",
        "20260816",
        "20260817",
        "20260818",
        "20260819",
    )


def test_empty_window_is_rejected() -> None:
    """빈 창은 '볼 것이 없었다'로 위장한다."""
    with pytest.raises(ValueError, match="1 이상"):
        lookback_dates(KST_MORNING, 0)


def test_naive_datetime_is_rejected() -> None:
    """타임존 없는 시각으로 달력을 계산하지 않는다 (원칙 6)."""
    with pytest.raises(ValueError, match="naive"):
        lookback_dates(dt.datetime(2026, 8, 20, 7, 0), 7)  # noqa: DTZ001 — 거부 대상을 만든다


def test_month_boundary_is_crossed() -> None:
    """월 경계에서 끊기지 않는다."""
    assert lookback_dates(dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.UTC), 3) == (
        "20260226",
        "20260227",
        "20260228",
    )


# ---------------------------------------------------------------------------
# 실행 상태 판정
# ---------------------------------------------------------------------------


def test_canary_failure_outranks_everything() -> None:
    """미수행은 실패가 아니다. 뒤의 관측값이 없으므로 먼저 판정한다."""
    assert (
        derive_status(canary_passed=False, probes=(), outcomes=())
        is OpsRunStatus.SKIPPED_CANARY_FAILED
    )


def test_all_dates_failed_is_a_failed_run() -> None:
    """카나리아가 보지 못하는 틈 — 날짜 요청이 전부 실패한 경우다."""
    probes = (_probe(IngestRunStatus.FAILED), _probe(IngestRunStatus.FAILED_ZERO_UNCONFIRMED))
    assert derive_status(canary_passed=True, probes=probes, outcomes=()) is OpsRunStatus.FAILED


def test_one_failed_law_makes_the_run_partial_not_failed() -> None:
    """**한 법령의 실패가 하루치를 날리지 않는다.** 그 결정이 이 값으로 드러난다."""
    probes = (_probe(IngestRunStatus.SUCCEEDED),)
    outcomes = (_outcome(LawOutcomeStatus.DIFFED), _outcome(LawOutcomeStatus.FAILED))
    assert (
        derive_status(canary_passed=True, probes=probes, outcomes=outcomes) is OpsRunStatus.PARTIAL
    )


def test_one_failed_date_also_makes_the_run_partial() -> None:
    """날짜 일부 실패도 부분 실패다. 나머지 날짜는 계속 폴링했다."""
    probes = (_probe(IngestRunStatus.SUCCEEDED), _probe(IngestRunStatus.FAILED))
    assert derive_status(canary_passed=True, probes=probes, outcomes=()) is OpsRunStatus.PARTIAL


def test_nothing_to_process_is_zero_not_success() -> None:
    """코퍼스 대상 0건은 정상이며, 연속 0건 알람이 세는 값이다."""
    probes = (_probe(IngestRunStatus.SUCCEEDED_ZERO),)
    assert (
        derive_status(canary_passed=True, probes=probes, outcomes=()) is OpsRunStatus.SUCCEEDED_ZERO
    )


def test_only_already_processed_laws_is_still_zero() -> None:
    """재확인 창이 이미 처리한 것만 다시 만난 날은 새로 포착한 것이 없다."""
    probes = (_probe(IngestRunStatus.SUCCEEDED),)
    outcomes = (_outcome(LawOutcomeStatus.SKIPPED_DONE),)
    assert (
        derive_status(canary_passed=True, probes=probes, outcomes=outcomes)
        is OpsRunStatus.SUCCEEDED_ZERO
    )


def test_enacted_law_counts_as_processed() -> None:
    """제정본은 diff 가 없지만 **처리한 것**이다. 0건과 구별된다."""
    probes = (_probe(IngestRunStatus.SUCCEEDED),)
    outcomes = (_outcome(LawOutcomeStatus.NO_PREVIOUS),)
    assert (
        derive_status(canary_passed=True, probes=probes, outcomes=outcomes)
        is OpsRunStatus.SUCCEEDED
    )
