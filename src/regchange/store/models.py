"""적재 결과 모델 — 처분(disposition)과 건수 단언.

목적:
    파싱된 조문단위 하나가 어떻게 처리됐는지를 서로 겹치지 않는 처분으로 나누고,
    처분별 건수의 합이 파싱된 수와 같은지 단언한다.

구현 이유:
    지난 페이지네이션 결함은 "누적 건수만 보고 누적된 것이 서로 다른가는 아무도
    보지 않았다"였다. 여기서 가능한 같은 계열의 함정은 **일부를 아예 세지 않는
    것**이다. 소관부처가 마스터에 없는 조문을 그냥 적재하고 `loaded` 에 더하면,
    미해결이라는 사실이 적재 건수에 흡수되어 사라진다. 아무 단언도 깨지지 않고
    합계도 맞는다.

    그래서 처분을 **상호 배타**로 정의한다. 한 단위는 정확히 하나의 처분을 받는다.
    적재됐지만 부처가 미해결인 단위는 `LOADED` 가 아니라 `LOADED_UNRESOLVED` 다.
    DB 에 들어간 행 수는 두 값의 합이며 `rows_in_db` 가 그것을 따로 제공한다.

트레이드오프:
    "적재된 행 수"를 알려면 두 필드를 더해야 한다. `loaded` 하나만 보면 실제보다
    적게 세게 되므로, 이 설계는 새로운 오독 경로를 하나 만든다. 그 대신 미해결이
    합계 안에서 사라지는 경로를 없앴다. 둘 중 드러나는 쪽 오류를 택했다 —
    적게 세는 것은 이상하게 보이지만, 흡수되어 사라지는 것은 정상으로 보인다.

    처분을 4종으로 고정했으므로 새 실패 유형이 생기면 이 enum 과 CHECK 제약을
    함께 고쳐야 한다. 자유 문자열로 두면 그 부담은 없지만, 합이 맞는지 검사할
    대상 집합이 정의되지 않는다.

엣지 케이스:
    - `parsed_units` 가 0: `LawDocument` 는 조문단위 0건이면 파서가 이미 실패시킨다
      (ADR-005). 여기까지 0이 오면 파서 계약이 깨진 것이므로 단언에서 드러난다.
    - 키 충돌이 발생한 문서: 트랜잭션이 롤백되므로 그 문서의 단위는 DB 에 없다.
      충돌한 단위 하나만 `KEY_CONFLICT` 로 세고 run 을 중단한다 — 나머지 단위의
      처분을 지어내지 않는다.
    - 합이 맞지 않는 경우: `AssertionError` 가 아니라 `LoadError` 를 던진다.
      `python -O` 로 assert 가 꺼져도 검사가 사라지지 않아야 한다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class LoadError(RuntimeError):
    """적재 경로의 실패. 조용한 기본값 대신 호출자에게 실패를 알린다."""


class KeyConflictError(LoadError):
    """식별키가 충돌했다. 조용히 덮어쓰지 않고 여기서 멈춘다.

    목적:
        같은 식별키로 **내용이 다른** 행을 적재하려는 시도를 실패로 만든다.

    구현 이유:
        edge-case #18 은 진짜 중복이 35,681행에서 0건이라고 기록하되,
        "충돌 0건이므로 안전하다"가 아니라 "충돌 0건이지만 런타임 단언으로
        감시한다"로 결론지었다. 큰 표본에 없다는 것이 없다는 뜻이 아니기 때문이다.
        이 예외가 발화하는 날이 곧 식별키를 다시 검토할 날이다.

    트레이드오프:
        충돌 하나가 run 전체를 중단시킨다. 나머지 문서의 적재가 지연된다. 그 대신
        "키가 틀렸을지 모른다"는 신호를 무시한 채로 데이터가 더 쌓이지 않는다.
        키가 틀린 상태로 쌓인 데이터는 원칙 6 때문에 지울 수도 없다.

    엣지 케이스:
        - 내용까지 같은 경우: 충돌이 아니라 멱등 재적재다. `SKIPPED` 로 처리하며
          이 예외를 던지지 않는다.
    """

    def __init__(
        self,
        key: tuple[object, ...],
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        """충돌한 식별키와 두 행의 전체 필드를 예외에 담는다."""
        self.key = key
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"식별키 충돌: {key}\n"
            f"  기존 행: {existing}\n"
            f"  신규 행: {incoming}\n"
            "조용히 덮어쓰지 않는다. 이 단언이 발화했다면 식별키를 다시 검토한다 (edge-case #18)."
        )


class Disposition(StrEnum):
    """파싱된 조문단위 하나가 받은 처분. 상호 배타적이다."""

    LOADED = "LOADED"
    """적재됨. 소관부처가 마스터에서 해결됐다."""

    LOADED_UNRESOLVED = "LOADED_UNRESOLVED"
    """적재됨. 소관부처가 마스터에 없어 미해결로 기록됐다 (ADR-009)."""

    SKIPPED = "SKIPPED"
    """식별키가 이미 있어 건너뛰었다. 멱등 재적재의 정상 경로다."""

    KEY_CONFLICT = "KEY_CONFLICT"
    """식별키는 같은데 내용이 다르다. 실패이며 run 을 중단시킨다."""


@dataclass(frozen=True, slots=True)
class LoadCounts:
    """처분별 건수. 넷의 합이 `parsed_units` 와 같아야 한다."""

    parsed_units: int = 0
    loaded: int = 0
    loaded_unresolved: int = 0
    skipped: int = 0
    key_conflicts: int = 0

    @property
    def rows_in_db(self) -> int:
        """실제로 DB 에 들어간 행 수. `loaded` 하나만 보면 미해결 건이 빠진다."""
        return self.loaded + self.loaded_unresolved

    @property
    def dispositioned(self) -> int:
        """처분이 부여된 단위 수."""
        return self.loaded + self.loaded_unresolved + self.skipped + self.key_conflicts

    def with_disposition(self, disposition: Disposition) -> LoadCounts:
        """처분 하나를 더한 새 건수를 만든다. 원본을 바꾸지 않는다."""
        return LoadCounts(
            parsed_units=self.parsed_units,
            loaded=self.loaded + (disposition is Disposition.LOADED),
            loaded_unresolved=self.loaded_unresolved
            + (disposition is Disposition.LOADED_UNRESOLVED),
            skipped=self.skipped + (disposition is Disposition.SKIPPED),
            key_conflicts=self.key_conflicts + (disposition is Disposition.KEY_CONFLICT),
        )

    def merge(self, other: LoadCounts) -> LoadCounts:
        """두 건수를 합친다. 문서별 결과를 run 단위로 모을 때 쓴다."""
        return LoadCounts(
            parsed_units=self.parsed_units + other.parsed_units,
            loaded=self.loaded + other.loaded,
            loaded_unresolved=self.loaded_unresolved + other.loaded_unresolved,
            skipped=self.skipped + other.skipped,
            key_conflicts=self.key_conflicts + other.key_conflicts,
        )

    def verify_partition(self, *, context: str) -> None:
        """처분별 건수의 합이 파싱된 수와 같은지 검사한다.

        목적:
            "일부를 아예 세지 않는" 실패를 드러낸다.

        구현 이유:
            `assert` 를 쓰지 않는다. `python -O` 로 실행하면 assert 는 통째로
            사라지고, 그때 이 검사는 있는 것처럼 보이면서 돌지 않는다. 규제
            도메인에서 "돌지 않는 검사"는 검사가 없는 것보다 나쁘다 — 있다고
            믿게 만들기 때문이다.

        트레이드오프:
            정상 경로에서도 매번 도는 비용이 있다. 합 하나를 비교하는 비용이므로
            무시할 수 있고, 실패했을 때 얻는 정보가 훨씬 크다.

        엣지 케이스:
            - 합이 크다: 한 단위가 두 번 세어졌다. 처분이 상호 배타가 아니게 된 것이다.
            - 합이 작다: 세지 않은 단위가 있다. 이쪽이 조용한 누락이다.
        """
        if self.dispositioned != self.parsed_units:
            raise LoadError(
                f"적재 건수 단언 실패 ({context}): "
                f"적재 {self.loaded} + 미해결적재 {self.loaded_unresolved} "
                f"+ 건너뜀 {self.skipped} + 충돌 {self.key_conflicts} "
                f"= {self.dispositioned} 인데 파싱된 단위는 {self.parsed_units} 다"
            )


@dataclass(frozen=True, slots=True)
class DocumentLoadResult:
    """문서 하나의 적재 결과. 트랜잭션 경계와 같은 단위다."""

    document_id: UUID
    law_id: str
    mst: str
    document_effective_date: dt.date
    counts: LoadCounts
    unresolved_ministry: str | None = None
    """마스터에서 해결되지 않은 소관부처 관측값. 해결됐으면 None 이다."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """적재 run 하나의 결과.

    `load_run_id` 가 None 이면 `load_run` 행이 기록되지 않았다는 뜻이며, 곧
    이 run 은 완료되지 않았다. 매니페스트가 없는 스냅샷과 같은 상태다.
    """

    source_key: str
    source_run_id: str
    started_at: dt.datetime
    completed_at: dt.datetime | None
    counts: LoadCounts
    documents: tuple[DocumentLoadResult, ...] = field(default_factory=tuple)
    load_run_id: UUID | None = None

    @property
    def complete(self) -> bool:
        """`load_run` 행이 기록됐는가. 그 존재가 곧 완료를 의미한다."""
        return self.load_run_id is not None
