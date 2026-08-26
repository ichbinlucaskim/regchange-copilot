"""킬 스위치를 켜고 끈다 — 사람의 행위이며 기록이 남는다 (5단계).

목적:
    `kill_switch` 의 현재 행을 닫고 새 행을 넣는다. 그리고 현재 상태와 이력을 읽는다.

구현 이유:
    **쓰기를 `ops` 에 둔 이유는 이것이 운영 행위이기 때문이다.** 스위치를 끄는 것은
    담당자가 상황을 보고 내리는 결정이며, 어떤 서비스 프로세스도 스스로 하지 않는다.
    그래서 이 함수는 **소유자 커넥션**을 받는다 — `app_graph`·`app_dispatch` 에는
    이 테이블에 대한 INSERT 권한이 아예 없다 (마이그레이션 013).

    **읽기는 `adapters/switches.py` 에 따로 있다.** 같은 테이블을 두 곳에서 다루는 것이
    중복처럼 보이지만, 방향이 다르다 — 읽기는 LLM 경로가 매 호출 하는 일이고 쓰기는
    사람이 가끔 하는 일이다. 한 클래스에 합치면 **LLM 경로가 들고 다니는 객체에 쓰기
    메서드가 생긴다.** 권한이 먼저 막지만, 막힌 것을 부르는 코드를 두지 않는 것이
    원칙 5 의 방식이다.

    **닫고-새로 넣는다** (원칙 6). 값을 덮으면 "3월 15일에 이 스위치가 켜져 있었나"에
    답할 수 없다. 감사에서 "왜 그날 분석이 안 돌았나"의 답이 이 이력이다.

    **`changed_by` 와 `reason` 을 필수 인자로 받는다.** 기본값을 두면 기본값이 쓰인다.
    한 줄 쓰는 마찰이 방어다 — 이유를 적어야 하면 함부로 끄지 않는다.

트레이드오프:
    - 같은 값으로 다시 설정해도 행이 하나 쌓인다. 막지 않는 이유는 **두 번째 이유가
      첫 번째와 다를 수 있기** 때문이다. "잠시 껐다 켠다"와 "계속 꺼 둔다"는 다른 사실이고,
      그 차이는 `reason` 문장에만 있다.
    - 닫기와 넣기가 한 트랜잭션이다. 나누면 "열린 행이 0개" 또는 "2개"인 순간이 생기고,
      전자는 `NEVER_SET`(꺼짐)으로 읽혀 조용히 기능이 멈춘다.

엣지 케이스:
    - **현재 행이 없음**: 닫을 것이 없으므로 넣기만 한다. 첫 설정이 이 경로다.
    - **알 수 없는 이름**: DB 의 CHECK 가 거부한다. 코드에서 한 번 더 막는 이유는
      오류 메시지 때문이다 — CHECK 위반 메시지는 어떤 값이 허용되는지 알려주지 않는다.
    - **빈 이유/빈 사람**: `ValueError`. DB CHECK 도 막지만 같은 이유로 앞에서 막는다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg

from regchange.adapters.switches import SwitchState
from regchange.guards.killswitch import Switch

CLOSE_CURRENT = """
UPDATE kill_switch SET known_until = %(now)s
 WHERE name = %(name)s AND known_until = 'infinity'
"""

INSERT_ROW = """
INSERT INTO kill_switch (id, name, enabled, changed_by, reason, known_from)
VALUES (%(id)s, %(name)s, %(enabled)s, %(changed_by)s, %(reason)s, %(now)s)
"""

CURRENT_ALL = """
SELECT name, enabled, changed_by, reason, known_from
  FROM kill_switch
 WHERE known_until = 'infinity'
 ORDER BY name
"""

HISTORY = """
SELECT name, enabled, changed_by, reason, known_from, known_until
  FROM kill_switch
 WHERE (%(name)s::text IS NULL OR name = %(name)s::text)
 ORDER BY known_from DESC, name
 LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class SwitchChange:
    """이력 한 줄. 닫힌 행이면 `known_until` 이 무한이 아니다."""

    name: str
    enabled: bool
    changed_by: str
    reason: str
    known_from: dt.datetime
    known_until: dt.datetime


async def set_switch(
    conn: psycopg.AsyncConnection[Any],
    *,
    switch: Switch,
    enabled: bool,
    changed_by: str,
    reason: str,
    now: dt.datetime | None = None,
) -> None:
    """스위치 값을 바꾸고 이력을 남긴다 (**소유자 커넥션이어야 한다**).

    목적:
        현재 행을 닫고 새 행을 한 트랜잭션 안에서 넣는다.

    구현 이유:
        `now` 를 주입받을 수 있게 둔 이유는 테스트다. 기본값은 호출 시각이며, 운영에서
        이 인자를 넘기지 않는다 — 넘길 수 있게 두면 이력의 시각이 조작 가능해 보인다.
        그래서 인자 이름을 `now` 로 두고 기본값을 실제 시각으로 고정했다.

    트레이드오프:
        같은 값으로 다시 설정해도 막지 않는다. 모듈 docstring 참조.

    엣지 케이스:
        모듈 docstring 참조.
    """
    if not changed_by.strip():
        msg = "changed_by 가 비어 있다. 누가 바꿨는지 없는 기록은 이력이 아니다"
        raise ValueError(msg)
    if not reason.strip():
        msg = "reason 이 비어 있다. 껐다는 사실만 남으면 나중에 왜 껐는지를 모른다"
        raise ValueError(msg)

    stamp = now or dt.datetime.now(dt.UTC)
    params = {
        "id": uuid4(),
        "name": switch.value,
        "enabled": enabled,
        "changed_by": changed_by.strip(),
        "reason": reason.strip(),
        "now": stamp,
    }
    async with conn.transaction():
        await conn.execute(CLOSE_CURRENT, {"name": switch.value, "now": stamp})
        await conn.execute(INSERT_ROW, params)


async def current_switches(conn: psycopg.AsyncConnection[Any]) -> dict[str, SwitchState]:
    """열린 행 전부. **목록에 없는 스위치는 꺼짐이다** (설정된 적이 없다)."""
    cur = await conn.execute(CURRENT_ALL)
    rows: list[Any] = list(await cur.fetchall())
    return {
        row[0]: SwitchState(
            name=row[0], enabled=row[1], changed_by=row[2], reason=row[3], changed_at=row[4]
        )
        for row in rows
    }


async def switch_history(
    conn: psycopg.AsyncConnection[Any],
    *,
    switch: Switch | None = None,
    limit: int = 20,
) -> list[SwitchChange]:
    """변경 이력. 최근 것부터 돌려준다."""
    cur = await conn.execute(HISTORY, {"name": switch.value if switch else None, "limit": limit})
    rows: list[Any] = list(await cur.fetchall())
    return [
        SwitchChange(
            name=row[0],
            enabled=row[1],
            changed_by=row[2],
            reason=row[3],
            known_from=row[4],
            known_until=row[5],
        )
        for row in rows
    ]
