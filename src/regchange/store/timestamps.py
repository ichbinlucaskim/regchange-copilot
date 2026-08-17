"""`infinity` timestamptz 를 파이썬으로 실어 나른다.

목적:
    `known_until = 'infinity'`(= 아직 닫히지 않은 행)를 파이썬에서 읽을 수 있게 한다.

구현 이유:
    psycopg3 는 무한 timestamptz 를 만나면 `DataError: timestamp too large` 를
    던진다. 파이썬 `datetime` 에 무한이 없기 때문이다. 그래서 `datetime.max` /
    `datetime.min` 으로 옮긴다.

    **`'infinity'` 대신 `9999-12-31` 같은 센티널을 컬럼에 넣는 대안을 택하지 않았다.**
    센티널은 값처럼 보이는 비-값이라 산술과 비교에 그대로 끼어든다 — "언제까지
    유효한가"를 집계하면 9999년이 평균에 들어가고, 그 오염은 예외 없이 조용히
    퍼진다. 무한은 DB 가 무한으로 다루고, 경계에서 한 번만 번역하는 편이 낫다.

    `valid_to` 는 반대로 `9999-12-31` 을 쓴다. 두 축의 취급이 다른 것은 의도적이다 —
    `valid_to` 는 법정 달력의 날짜이고 "무한히 유효한 조문"은 법적으로 존재하지
    않는다(언젠가 폐지된다). `known_until` 은 우리 시스템의 인지 구간이고, 아직
    닫히지 않았다는 것은 진짜로 끝이 없다는 뜻이다.

트레이드오프:
    프로세스 전역 어댑터를 등록하므로 이 패키지를 import 하는 것만으로 다른 모듈의
    timestamptz 읽기 동작도 바뀐다. 커넥션마다 등록하는 대안은 등록을 잊은 커넥션을
    하나 남기고, 그 하나는 평소엔 잘 돌다가 **닫히지 않은 행을 읽는 순간에만**
    터진다. 조용히 다르게 동작하는 것보다 전역이 낫다.

    `datetime.max` 는 진짜 무한이 아니다. `known_until > x` 비교는 x 가 9999년을
    넘지 않는 한 같은 답을 준다. 시간 조건은 전부 DB 에서 평가되므로 파이썬 쪽
    비교는 표시와 단언에만 쓰인다.

엣지 케이스:
    - `-infinity`: `datetime.min` 으로 옮긴다. 이 스키마는 쓰지 않지만, 만났을 때
      예외로 죽는 것보다 값이 보이는 편이 진단에 낫다.
    - 유한한 값: psycopg 기본 동작 그대로다.
    - 중복 등록: 같은 로더를 다시 등록해도 psycopg 가 덮어쓴다. 부작용이 없다.
"""

from __future__ import annotations

import datetime as dt

import psycopg
from psycopg.abc import Buffer
from psycopg.types.datetime import TimestamptzLoader

POSITIVE_INFINITY = dt.datetime.max.replace(tzinfo=dt.UTC)
"""`'infinity'` 의 파이썬 표현. 열린 행의 `known_until` 이 이 값으로 읽힌다."""

NEGATIVE_INFINITY = dt.datetime.min.replace(tzinfo=dt.UTC)

_INFINITY = b"infinity"
_NEGATIVE_INFINITY = b"-infinity"


class InfinityTimestamptzLoader(TimestamptzLoader):
    """무한 timestamptz 를 `datetime.max`/`datetime.min` 으로 읽는다."""

    def load(self, data: Buffer) -> dt.datetime:
        """무한이면 경계값으로, 아니면 기본 동작으로 읽는다."""
        raw = bytes(data)
        if raw == _INFINITY:
            return POSITIVE_INFINITY
        if raw == _NEGATIVE_INFINITY:
            return NEGATIVE_INFINITY
        return super().load(data)


def register_infinity_timestamps() -> None:
    """전역 어댑터에 로더를 등록한다. `regchange.store` import 시 한 번 호출된다."""
    psycopg.adapters.register_loader("timestamptz", InfinityTimestamptzLoader)
