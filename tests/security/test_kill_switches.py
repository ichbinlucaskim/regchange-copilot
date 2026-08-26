"""킬 스위치 발화 테스트 — **꺼면 실제로 안 도는가** (CLAUDE.md §6, 5단계).

이 테스트가 막는 위협:
    - 스위치가 **있는데 안 먹는 것.** 설정만 있고 검사 지점이 빠진 경로가 있으면,
      운영자는 껐다고 믿고 시스템은 계속 돈다. 그 어긋남은 요금 청구서나 발송된
      티켓으로 드러난다 — 즉 되돌릴 수 없는 방식으로 드러난다.
    - 설정 누락이 **기능 활성**으로 이어지는 것. 기본값은 꺼짐이어야 한다.
    - 상태를 읽지 못했을 때 **계속 도는 것.** 무언가 잘못돼서 멈추려는데 그 무언가
      때문에 스위치를 못 읽는 상황이 정확히 스위치가 필요한 순간이다.
    - "꺼져서 안 했다"와 "실패해서 못 했다"가 **같은 값으로 뭉개지는 것.** 조치가 다르다.
    - LLM 경로가 스위치를 **켤 수 있게** 되는 것. 인젝션이 성립해도 스위치는 못 켠다.

**이 테스트가 검사하지 못하는 것**: `DISPATCH_ENABLED` 는 관문까지만 검사한다.
발송 워커가 아직 없으므로 "워커가 실제로 안 보내는가"는 검사 대상이 없다 —
그 한계를 여기 적어 둔다. 적어 두지 않으면 6단계에 "이미 검증됐다"로 읽힌다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from regchange.adapters.switches import PostgresSwitchStore, SwitchState
from regchange.dispatch.gate import require_dispatch_enabled
from regchange.guards.killswitch import (
    CACHE_TTL_SECONDS,
    KillSwitchError,
    StopKind,
    Switch,
    SwitchGate,
    SwitchNeverSetError,
    SwitchOffError,
    SwitchUnknownError,
    static_gate,
)
from regchange.ops.switches import current_switches, set_switch, switch_history
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval.models import SearchMode
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole

pytestmark = [pytest.mark.security]

NOW = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.UTC)

ARTICLE = AmendedArticle(
    law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    article_path="제48조의3",
    revision_kind="일부개정",
    change_type="MODIFIED",
    after_text="24시간 이내에 신고하여야 한다.",
)


class ExplodingConnection:
    """건드리면 터지는 커넥션. **스위치가 DB 앞에서 멈췄는지**를 증명한다."""

    def __getattr__(self, name: str) -> Any:
        msg = f"스위치가 꺼졌는데 DB 를 건드렸다 (conn.{name})"
        raise AssertionError(msg)


class ExplodingLLM:
    """부르면 터지는 모델. **스위치가 호출 앞에서 멈췄는지**를 증명한다."""

    model_id = "exploding"

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        msg = "스위치가 꺼졌는데 모델을 불렀다"
        raise AssertionError(msg)


class CountingStore:
    """조회 횟수를 세는 저장소. 캐시 동작을 검사한다."""

    def __init__(self, state: SwitchState | None) -> None:
        self.state = state
        self.reads = 0

    async def read(self, name: str) -> SwitchState | None:  # noqa: ARG002
        self.reads += 1
        return self.state


class FailingStore:
    """조회가 실패하는 저장소. DB 장애를 흉내 낸다."""

    def __init__(self, *, fail_times: int, state: SwitchState | None = None) -> None:
        self.remaining = fail_times
        self.state = state
        self.reads = 0

    async def read(self, name: str) -> SwitchState | None:  # noqa: ARG002
        self.reads += 1
        if self.remaining > 0:
            self.remaining -= 1
            msg = "connection refused"
            raise ConnectionError(msg)
        return self.state


def _state(name: str, *, enabled: bool) -> SwitchState:
    return SwitchState(
        name=name,
        enabled=enabled,
        changed_by="lucas",
        reason="시험",
        changed_at=NOW,
    )


# ---------------------------------------------------------------------------
# 기본값 — 꺼짐 (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("switch", list(Switch))
async def test_default_is_off_for_every_switch(switch: Switch) -> None:
    """설정된 적이 없으면 꺼짐이다. **설정 누락이 기능 활성이 되지 않는다.**"""
    gate = static_gate({})

    with pytest.raises(SwitchNeverSetError) as caught:
        await gate.require(switch)

    assert caught.value.kind is StopKind.NEVER_SET


def test_every_switch_has_a_firing_test() -> None:
    """스위치 3종 전부에 발화 테스트가 있다 — 목록이 늘면 이 테스트가 먼저 깨진다."""
    covered = {Switch.RETRIEVAL, Switch.LLM, Switch.DISPATCH}
    assert covered == set(Switch), "스위치가 늘었는데 발화 테스트가 없다"


# ---------------------------------------------------------------------------
# 발화 — 꺼면 그 기능이 실제로 안 돈다
# ---------------------------------------------------------------------------


async def test_retrieval_switch_stops_before_touching_the_database() -> None:
    """`RETRIEVAL_ENABLED` 가 꺼지면 **DB 질의 전에** 멈춘다."""
    gate = static_gate({Switch.RETRIEVAL: False})

    with pytest.raises(SwitchOffError) as caught:
        await search(
            ExplodingConnection(),  # type: ignore[arg-type]
            switches=gate,
            query="침해사고 신고 기한",
            mode=SearchMode.HYBRID,
            limit=10,
        )

    assert caught.value.switch is Switch.RETRIEVAL
    assert "test-fixture" in str(caught.value), "누가 껐는지가 메시지에 있어야 한다"


async def test_retrieval_switch_is_checked_before_input_validation() -> None:
    """꺼진 기능이 **입력 형식을 탓하지 않는다.** 빈 질의여도 스위치 오류가 먼저다."""
    gate = static_gate({Switch.RETRIEVAL: False})

    with pytest.raises(SwitchOffError):
        await search(
            ExplodingConnection(),  # type: ignore[arg-type]
            switches=gate,
            query="   ",
            mode=SearchMode.HYBRID,
            limit=10,
        )


async def test_llm_switch_stops_before_calling_the_model() -> None:
    """`LLM_ENABLED` 가 꺼지면 **모델을 부르기 전에** 멈춘다."""
    gate = static_gate({Switch.RETRIEVAL: True, Switch.LLM: False})

    with pytest.raises(SwitchOffError) as caught:
        await extract_obligations(
            ExplodingConnection(),  # type: ignore[arg-type]
            switches=gate,
            article=ARTICLE,
            llm=ExplodingLLM(),
            embedding=MagicMock(),
            store=MagicMock(),
            as_of=dt.date(2026, 2, 1),
        )

    assert caught.value.switch is Switch.LLM


async def test_dispatch_switch_stops_the_gate() -> None:
    """`DISPATCH_ENABLED` 가 꺼지면 발송 관문이 멈춘다.

    **한계**: 발송 워커가 없으므로 여기까지가 검사 대상이다 (모듈 docstring 참조).
    """
    gate = static_gate({Switch.DISPATCH: False})

    with pytest.raises(SwitchOffError) as caught:
        await require_dispatch_enabled(gate)

    assert caught.value.switch is Switch.DISPATCH


async def test_dispatch_is_off_by_default_even_when_others_are_on() -> None:
    """검색·LLM 이 켜져 있어도 발송은 따로다. **켜는 것은 각각의 결정이다.**"""
    gate = static_gate({Switch.RETRIEVAL: True, Switch.LLM: True})

    with pytest.raises(SwitchNeverSetError):
        await require_dispatch_enabled(gate)


# ---------------------------------------------------------------------------
# 꺼진 것과 못 읽은 것을 구별한다
# ---------------------------------------------------------------------------


async def test_unreadable_state_stops_but_is_a_different_fact() -> None:
    """상태를 못 읽으면 멈추되 **`OFF` 가 아니라 `UNAVAILABLE`** 이다."""
    gate = SwitchGate(store=FailingStore(fail_times=1))

    with pytest.raises(SwitchUnknownError) as caught:
        await gate.require(Switch.LLM)

    assert caught.value.kind is StopKind.UNAVAILABLE
    assert isinstance(caught.value.cause, ConnectionError)
    assert isinstance(caught.value, KillSwitchError), "호출부가 한 번에 잡을 수 있어야 한다"


async def test_three_stop_kinds_are_distinguishable() -> None:
    """세 이유가 서로 다른 값으로 남는다. 뭉치면 DB 장애가 운영자의 결정처럼 보인다."""
    off = static_gate({Switch.LLM: False})
    never = static_gate({})
    broken = SwitchGate(store=FailingStore(fail_times=1))

    kinds = []
    for gate in (off, never, broken):
        with pytest.raises(KillSwitchError) as caught:
            await gate.require(Switch.LLM)
        kinds.append(caught.value.kind)

    assert kinds == [StopKind.OFF, StopKind.NEVER_SET, StopKind.UNAVAILABLE]


async def test_failures_are_not_cached() -> None:
    """조회 실패를 캐시하면 **복구된 뒤에도 더 멈춘다.** 장애 시간을 우리가 늘리지 않는다."""
    store = FailingStore(fail_times=1, state=_state(Switch.LLM.value, enabled=True))
    gate = SwitchGate(store=store)

    with pytest.raises(SwitchUnknownError):
        await gate.require(Switch.LLM)
    state = await gate.require(Switch.LLM)

    assert state.enabled is True
    assert store.reads == 2, "실패가 캐시됐다면 두 번째 조회가 없었을 것이다"


# ---------------------------------------------------------------------------
# 반영 — 재배포 없이, 최대 60초
# ---------------------------------------------------------------------------


def test_cache_ttl_matches_the_requirement() -> None:
    """캐시 수명이 요건(60초)과 같다. 이 값은 취향이 아니라 요건 그 자체다."""
    assert CACHE_TTL_SECONDS == 60.0


async def test_repeated_checks_hit_the_cache() -> None:
    """같은 창 안에서는 조회가 한 번이다 — 매 호출 접속하면 스위치가 부하가 된다."""
    store = CountingStore(_state(Switch.LLM.value, enabled=True))
    gate = SwitchGate(store=store)

    for _ in range(5):
        await gate.require(Switch.LLM)

    assert store.reads == 1


async def test_value_change_is_picked_up_without_restart() -> None:
    """캐시가 만료되면 **재시작 없이** 새 값이 반영된다.

    TTL 을 0 으로 두어 60초를 기다리지 않는다 — 시간을 재는 것이 아니라 **만료 후
    다시 읽는가**를 검사한다.
    """
    store = CountingStore(_state(Switch.LLM.value, enabled=True))
    gate = SwitchGate(store=store, ttl_seconds=0.0)

    await gate.require(Switch.LLM)
    store.state = _state(Switch.LLM.value, enabled=False)

    with pytest.raises(SwitchOffError):
        await gate.require(Switch.LLM)
    assert store.reads == 2


# ---------------------------------------------------------------------------
# 기록 — 누가 언제 왜
# ---------------------------------------------------------------------------


def test_writing_requires_a_person_and_a_reason() -> None:
    """이유 없는 변경을 막는다. **한 줄 쓰는 마찰이 방어다.**"""
    import asyncio

    for by, reason in (("", "이유"), ("lucas", "  ")):
        with pytest.raises(ValueError, match="비어 있다"):
            asyncio.run(
                set_switch(
                    MagicMock(),
                    switch=Switch.LLM,
                    enabled=False,
                    changed_by=by,
                    reason=reason,
                )
            )


@pytest.mark.requires_db
async def test_switch_change_leaves_history_and_is_read_back(
    owner_conn: psycopg.AsyncConnection[Any],
    role_test_dsn: Any,
) -> None:
    """켜고 끈 이력이 남고, 어댑터가 현재 값을 읽어 온다 (원칙 6).

    감사 질문 "왜 그날 분석이 안 돌았나"의 답이 이 이력이다.
    """
    await set_switch(
        owner_conn,
        switch=Switch.LLM,
        enabled=True,
        changed_by="lucas",
        reason="정상 운영",
        now=NOW,
    )
    await set_switch(
        owner_conn,
        switch=Switch.LLM,
        enabled=False,
        changed_by="lucas",
        reason="비용 급증 확인 중",
        now=NOW + dt.timedelta(hours=1),
    )

    current = await current_switches(owner_conn)
    assert current[Switch.LLM.value].enabled is False
    assert current[Switch.LLM.value].reason == "비용 급증 확인 중"

    history = await switch_history(owner_conn, switch=Switch.LLM)
    assert [row.enabled for row in history[:2]] == [False, True], "과거 행이 남아 있어야 한다"

    store = PostgresSwitchStore(role_test_dsn(DbRole.GRAPH))
    state = await store.read(Switch.LLM.value)
    assert state is not None
    assert state.enabled is False, "LLM role 이 같은 값을 읽어야 한다"

    gate = SwitchGate(store=store)
    with pytest.raises(SwitchOffError):
        await gate.require(Switch.LLM)


@pytest.mark.requires_db
async def test_llm_role_cannot_flip_switches(
    role_connect: Any,
    owner_conn: psycopg.AsyncConnection[Any],
) -> None:
    """**인젝션이 성립해도 스위치를 켤 수 없다.** 경계는 코드가 아니라 DB 권한이다."""
    await set_switch(
        owner_conn,
        switch=Switch.LLM,
        enabled=False,
        changed_by="lucas",
        reason="시험",
        now=NOW,
    )

    conn = await role_connect(DbRole.GRAPH)
    for statement in (
        "INSERT INTO kill_switch (id, name, enabled, changed_by, reason, known_from) "
        "VALUES (gen_random_uuid(), 'LLM_ENABLED', true, 'x', 'y', now())",
        "UPDATE kill_switch SET enabled = true WHERE known_until = 'infinity'",
        "DELETE FROM kill_switch",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute(statement)

    cur = await conn.execute(
        "SELECT enabled FROM kill_switch WHERE name = 'LLM_ENABLED' AND known_until = 'infinity'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] is False, "권한 거부 후에도 값이 그대로여야 한다"


@pytest.mark.requires_db
async def test_dispatch_role_can_read_its_own_switch(role_connect: Any) -> None:
    """발송 워커는 **자기를 멈추는 스위치를 읽을 수 있어야** 멈출 수 있다.

    003 이 이 role 의 시야를 `action_outbox` 로 좁혔으므로, 이 한 테이블에 대한
    SELECT 가 실제로 열려 있는지 확인한다. 이 테이블은 승인 내용도 프롬프트도 담지
    않으므로 원칙 5 의 경계를 넓히지 않는다.
    """
    conn = await role_connect(DbRole.DISPATCH)
    await conn.execute("SELECT name, enabled FROM kill_switch")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with conn.transaction():
            await conn.execute("SELECT * FROM impact_assessment")
