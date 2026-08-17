"""적재 건수 단언 — 미해결이 적재 건수에 흡수되어 사라지지 않는지 검사한다.

이 테스트가 존재하는 이유: 지난 페이지네이션 결함은 "누적 건수만 보고 누적된 것이
서로 다른가는 아무도 보지 않았다"였다. 여기서 가능한 같은 계열의 함정은 **일부를
아예 세지 않는 것**이며, 그 경우 합계도 맞고 아무 예외도 나지 않는다.
"""

from __future__ import annotations

import pytest

from regchange.store.models import Disposition, LoadCounts, LoadError


def test_partition_holds_when_every_unit_is_dispositioned() -> None:
    """모든 단위가 처분을 받으면 단언이 통과한다."""
    counts = LoadCounts(parsed_units=3)
    counts = counts.with_disposition(Disposition.LOADED)
    counts = counts.with_disposition(Disposition.LOADED_UNRESOLVED)
    counts = counts.with_disposition(Disposition.SKIPPED)
    counts.verify_partition(context="테스트")


def test_unresolved_is_not_absorbed_into_loaded() -> None:
    """미해결 적재는 `loaded` 가 아니라 별도 항이다.

    둘을 합치면 "부처가 미해결이었다"는 사실이 적재 건수 안에서 사라진다.
    합계는 여전히 맞으므로 단언으로는 잡히지 않는다 — 그래서 처분을 나눴다.
    """
    counts = LoadCounts(parsed_units=2)
    counts = counts.with_disposition(Disposition.LOADED)
    counts = counts.with_disposition(Disposition.LOADED_UNRESOLVED)

    assert counts.loaded == 1
    assert counts.loaded_unresolved == 1
    assert counts.rows_in_db == 2, "DB 에 들어간 행은 둘 다이다"
    counts.verify_partition(context="테스트")


def test_uncounted_unit_is_detected() -> None:
    """세지 않은 단위가 있으면 실패한다. 이쪽이 조용한 누락이다."""
    counts = LoadCounts(parsed_units=3).with_disposition(Disposition.LOADED)
    with pytest.raises(LoadError, match="적재 건수 단언 실패"):
        counts.verify_partition(context="테스트")


def test_double_counted_unit_is_detected() -> None:
    """한 단위가 두 번 세어져도 실패한다. 처분이 상호 배타가 아니게 된 경우다."""
    counts = LoadCounts(parsed_units=1)
    counts = counts.with_disposition(Disposition.LOADED)
    counts = counts.with_disposition(Disposition.SKIPPED)
    with pytest.raises(LoadError, match="적재 건수 단언 실패"):
        counts.verify_partition(context="테스트")


def test_verify_partition_raises_not_asserts() -> None:
    """`assert` 가 아니라 예외를 던진다. `python -O` 에서도 검사가 살아 있어야 한다."""
    counts = LoadCounts(parsed_units=1)
    with pytest.raises(LoadError):
        counts.verify_partition(context="테스트")


def test_merge_sums_every_field() -> None:
    """문서별 건수를 run 단위로 합칠 때 어느 항도 빠지지 않는다."""
    left = LoadCounts(parsed_units=2, loaded=1, loaded_unresolved=1)
    right = LoadCounts(parsed_units=3, loaded=1, skipped=1, key_conflicts=1)
    merged = left.merge(right)

    assert merged.parsed_units == 5
    assert merged.dispositioned == 5
    merged.verify_partition(context="테스트")
