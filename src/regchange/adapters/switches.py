"""킬 스위치 조회 경계 — 상태가 어디에 저장되든 도메인은 같은 질문만 한다.

목적:
    "이 스위치가 지금 켜져 있는가"를 묻는 인터페이스와, 그 답을 Postgres 에서 읽는
    구현을 제공한다.

구현 이유:
    **Protocol 을 두는 이유는 AWS 이관이다** (ADR-010 과 같은 논리). 로컬·온프레미스
    에서는 `kill_switch` 테이블이지만, 배포 형태가 정해지면 SSM Parameter Store 나
    AppConfig 가 될 수 있다. 그 교체가 도메인 코드 수정이 되면 안 된다 — 스위치를
    옮기는 작업이 안전장치 코드를 건드리는 작업이 되기 때문이다.

    **Protocol 에 캐시를 넣지 않는다.** 캐시는 호출부(`guards.killswitch.SwitchGate`)의
    관심사다. SSM 은 자체 캐싱 특성이 다르고, 인터페이스가 캐시를 규정하면 그 특성과
    싸우게 된다. 여기서는 "지금 값을 읽어 온다"만 약속한다.

    **읽을 때마다 커넥션을 새로 연다.** 게이트가 최대 60초 캐시하므로 스위치당 분당
    한 번 수준이고, 그 대가로 이 어댑터가 커넥션 수명을 관리하지 않는다. 호출부의
    커넥션을 빌려 쓰지 않는 이유는 그 커넥션의 트랜잭션 상태에 스위치 조회가 얽히기
    때문이다 — 조회가 호출부의 롤백에 딸려 사라지면 안 된다.

    **읽기 전용이다.** 스위치를 켜고 끄는 것은 사람이 CLI 로 한다 (`ops/switches.py`).
    이 어댑터에 쓰기를 두면 LLM 경로가 들고 있는 객체에 쓰기 메서드가 생기고, 그것이
    인젝션이 스위치를 켜는 경로가 된다 — DB 권한이 먼저 막지만, **막힌 것을 부르는
    코드를 두지 않는 것**이 원칙 5 의 방식이다.

트레이드오프:
    - 조회마다 접속 비용이 든다(로컬 기준 수 ms). 캐시가 그것을 분당 1회로 줄인다.
    - 실패를 예외로 던지고 기본값을 돌려주지 않는다. 호출부가 "읽지 못했다"와
      "꺼져 있다"를 구별할 수 있어야 하며, 둘을 같은 값으로 뭉개면 DB 장애가
      운영자의 결정처럼 보인다.

엣지 케이스:
    - **행이 없음**: `None` 을 돌려준다. 예외가 아니다 — 아직 아무도 켜지 않은 것은
      정상 상태이며, 그 해석(꺼짐)은 게이트가 한다.
    - **닫힌 행만 있음**: 열린 행이 없으므로 `None` 이다. 스위치를 껐다 켰다 한 이력이
      있어도 현재 값은 열린 행 하나뿐이다.
    - **DB 접속 실패**: `psycopg` 예외를 그대로 전파한다. 게이트가 잡아 `SwitchUnknown`
      으로 바꾸며, 그 구별이 기록에 남는다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

CURRENT_QUERY = """
SELECT name, enabled, changed_by, reason, known_from
  FROM kill_switch
 WHERE name = %(name)s AND known_until = 'infinity'
"""


@dataclass(frozen=True, slots=True)
class SwitchState:
    """스위치 하나의 현재 값과 그 값이 된 경위.

    `reason` 과 `changed_by` 를 값에 실어 나른다 — 멈춘 이유를 로그와 예외 메시지에
    그대로 보여 주기 위해서다. "왜 안 도는가"를 담당자가 DB 를 열어 보고 알아내야 하면
    스위치가 운영 도구가 되지 못한다.
    """

    name: str
    enabled: bool
    changed_by: str
    reason: str
    changed_at: dt.datetime


class SwitchStore(Protocol):
    """스위치 현재 값을 읽는 경계. 쓰기는 없다."""

    async def read(self, name: str) -> SwitchState | None:
        """스위치 하나의 현재 값. 아직 설정된 적이 없으면 `None`."""
        ...


@dataclass(frozen=True, slots=True)
class PostgresSwitchStore:
    """`kill_switch` 테이블에서 읽는 구현.

    목적:
        열린 행 하나를 읽어 `SwitchState` 로 돌려준다.

    구현 이유:
        DSN 을 받는다. 접속할 role 을 이 클래스가 고르지 않는 이유는 프로세스마다
        다르기 때문이다 — 그래프는 `app_graph`, 발송 워커는 `app_dispatch` 로 읽는다.
        어느 role 로 붙든 이 테이블에는 SELECT 만 있다 (마이그레이션 013).

    트레이드오프:
        조회마다 접속한다. 모듈 docstring 참조.

    엣지 케이스:
        모듈 docstring 참조.
    """

    dsn: str

    async def read(self, name: str) -> SwitchState | None:
        """열린 행 하나를 읽는다. 없으면 `None`, 접속 실패는 전파한다."""
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            cur = await conn.execute(CURRENT_QUERY, {"name": name})
            row: Any = await cur.fetchone()
        if row is None:
            return None
        return SwitchState(
            name=row[0],
            enabled=row[1],
            changed_by=row[2],
            reason=row[3],
            changed_at=row[4],
        )
