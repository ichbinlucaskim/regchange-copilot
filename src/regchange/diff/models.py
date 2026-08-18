"""diff 입출력 모델 — 조문 스냅샷, 변경, 이동 후보.

목적:
    두 버전 비교의 입력(조문 스냅샷)과 출력(변경·이동 후보·건수)을 타입이 붙은
    값으로 표현한다.

구현 이유:
    입력을 `ArticleSnapshot` 이라는 **diff 고유 타입**으로 받는다. DB 행이나
    파서 모델을 그대로 받지 않는 이유는 원칙 1이다 — diff 는 I/O 의존성을 갖지
    않아야 하고(import 계약이 `regchange.store` 를 금지한다), 파서 모델을 받으면
    파싱 트리 전체를 들고 다니게 되어 "DB 에서 읽은 것"과 "방금 파싱한 것"이
    다른 코드 경로를 타게 된다. 스냅샷 하나로 좁히면 두 출처가 같은 경로를 탄다.

    `body_norm_sha256` 을 스냅샷에 포함시키고 diff 가 직접 조립하지 않는다. 조립
    규칙은 `parse.assemble` 한 곳이며(migration 005 참조), diff 가 다시 조립하면
    규칙이 둘로 갈린다.

트레이드오프:
    스냅샷으로 좁히면 diff 가 항/호/목 트리를 볼 수 없다. 따라서 "제2항이 바뀌었다"
    수준의 세분화된 변경은 이 계층에서 만들 수 없고, 조문 단위 판정까지만 한다.
    문단 단위 diff 가 필요해지면 스냅샷을 넓히는 것이 아니라 별도 계층을 둔다 —
    조문 단위 판정이 흔들리지 않게 하는 편이 낫다.

엣지 케이스:
    - `title` 이 None 일 수 있다. `조문제목` 은 조건부 태그다(특금법 34개 중 27개).
      제목 신호를 쓸 수 없다는 사실 자체를 evidence 에 남긴다 (ADR-003).
    - `body_norm` 이 빈 문자열일 수 있다. 껍데기 항만 가진 조문이 존재한다
      (4개 법령 51건). 유사도가 무의미하므로 후보에서 제외하고 그 사실을 기록한다.
    - `moves` 는 도착 버전에만 있다. 출발 버전 스냅샷의 `moves` 는 보통 비어 있다
      (특금법 2011판 0건 / 2020판 18건) — ADR-003 근거 (b).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from regchange.parse.models import MoveReference

DEFAULT_PRIORITY_RANK = 0
"""기본 우선순위. 값이 작을수록 먼저 본다."""

EDITORIAL_PRIORITY_RANK = 9
"""EDITORIAL 의 우선순위. **버리는 것이 아니라 최하위로 내리는 것이다.**

감사에서 "이 문구정비 개정은 왜 검토하지 않았나"를 물으면, 시스템이 인지했고
등급을 매겼으며 그 근거가 남아 있다고 답할 수 있어야 한다. 행을 만들지 않으면
그 답이 불가능해진다.

이 값이 곧 우선순위 정책은 아니다. 정책은 분석 계층이 정하며 여기 있는 것은
기계적 강등뿐이다.
"""

EXPLICIT_SCORE = 1.0
"""명시 표기의 점수. 1순위 근거지만 자동 확정하지는 않는다 (ADR-003)."""

TITLE_SCORE = 0.5
"""조문제목 완전일치의 점수. **고정값이며 튜닝 대상이 아니다.**

근거 데이터가 특금법 한 쌍뿐이라 지금 조정하면 한 사례에 과적합된다. 여러 법령
쌍으로 분포를 본 뒤 별도 ADR(adr-011, 번호 예약)에서 확정한다. 그때까지 이 값을
바꾸는 커밋은 근거 없이 임계값을 움직이는 것이다.
"""


class ChangeType(StrEnum):
    """조문 하나의 변경 유형. 상호 배타적이다."""

    ADDED = "ADDED"
    """to 에만 존재한다."""

    DELETED = "DELETED"
    """from 에만 존재한다."""

    MODIFIED = "MODIFIED"
    """조립본 해시가 다르다 — 실질 변경이다."""

    EDITORIAL = "EDITORIAL"
    """조립본 해시는 같고 개정 마커만 다르다.

    **R-14(가짜 알림 폭주 → 알림 무시)의 유일한 방어선이다.** 법령이 개정되면
    실질 내용이 그대로여도 마커의 날짜가 바뀐다. 정규화본으로 비교하지 않으면
    모든 조문이 변경으로 보고되고, 담당자는 수백 건의 가짜 알림을 받은 뒤
    알림 자체를 보지 않게 된다.
    """

    UNCHANGED = "UNCHANGED"
    """해시도 마커도 같다. `article_change` 행을 만들지 않는다."""


class EvidenceKind(StrEnum):
    """이동 후보의 1순위 근거 (ADR-003 구현 지침)."""

    EXPLICIT = "EXPLICIT"
    """`조문참고자료` 명시 표기. API 가 준 구조가 아니라 우리가 텍스트에서 추출한 것이다."""

    TITLE = "TITLE"
    """조문제목 완전일치."""

    SIMILARITY = "SIMILARITY"
    """`body_norm` 유사도."""


class Cardinality(StrEnum):
    """후보 관계. 1:1 이 아닌 것을 억지로 1:1 로 줄이지 않는다 (ADR-003)."""

    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"
    MANY_TO_MANY = "N:M"


@dataclass(frozen=True, slots=True)
class ArticleSnapshot:
    """비교 대상 조문 하나. DB 행과 파서 결과가 모두 이 형태로 좁혀진다."""

    article_id: UUID | None
    article_no: int
    branch_no: int
    title: str | None
    body_norm: str
    body_norm_sha256: str
    marker_signature: tuple[str, ...] = ()
    """개정 마커를 비교 가능한 형태로 편 것. 순서를 유지한다 — 어느 계층에 몇 개
    붙었는지가 EDITORIAL 판정의 신호이므로 집합으로 뭉개지 않는다."""

    moves: tuple[MoveReference, ...] = ()
    reference_raw: str | None = None

    @property
    def ref(self) -> tuple[int, int]:
        """`(article_no, branch_no)`. 문자열 `"제5조의2"` 로 짝짓지 않는다 (ADR-001)."""
        return (self.article_no, self.branch_no)

    @property
    def is_empty(self) -> bool:
        """본문이 비었는가. 유사도 계산에서 제외할 대상이다."""
        return not self.body_norm.strip()


@dataclass(frozen=True, slots=True)
class ArticleChange:
    """변경 하나."""

    change_type: ChangeType
    article_no: int
    branch_no: int
    from_article_id: UUID | None
    to_article_id: UUID | None
    priority_rank: int

    @property
    def ref(self) -> tuple[int, int]:
        """조문 좌표."""
        return (self.article_no, self.branch_no)


@dataclass(frozen=True, slots=True)
class MoveCandidate:
    """이동 후보 하나. `status` 는 언제나 PENDING 이므로 필드로 두지 않는다."""

    from_ref: tuple[int, int]
    to_ref: tuple[int, int]
    score: float
    evidence_kind: EvidenceKind
    evidence: dict[str, object]
    cardinality: Cardinality


@dataclass(frozen=True, slots=True)
class MoveWindow:
    """이동 표기를 걸러낼 날짜 창.

    `조문참고자료` 는 그 조문의 이동 이력 **전체**를 누적한다 — 실측에서 표기 128건
    중 문서 시행 연도와 같은 해가 0건이고, 소득세법 한 문서가 2006·2010·2018·2022
    네 시점의 이동을 함께 담는다. 창 없이 쓰면 2008년 이동이 오늘의 후보가 된다.

    경계는 **`from` 배타 / `to` 포함**이다. `to` 의 공포일에 일어난 이동은 이번
    diff 의 대상이고, `from` 의 공포일에 일어난 이동은 이전 diff 의 대상이다.
    """

    after: dt.date
    """`from` 문서의 공포일자. 이 날짜는 포함하지 않는다."""

    through: dt.date
    """`to` 문서의 공포일자. 이 날짜는 포함한다."""

    def contains(self, when: dt.date | None) -> bool:
        """이 날짜가 창 안인가. 날짜가 없는 표기는 창 밖으로 다룬다."""
        return when is not None and self.after < when <= self.through


@dataclass(frozen=True, slots=True)
class MoveMetrics:
    """이동 후보 생성 과정의 관측값.

    dict 로 돌려주지 않는 이유: 키 오타가 런타임까지 살아남고, 호출자가 캐스팅을
    해야 하며, 어떤 값이 들어 있는지 정의가 흩어진다. 관측값도 결과의 일부다.
    """

    moves_in_window: int = 0
    moves_out_of_window: int = 0
    out_of_window_dates: tuple[str, ...] = ()
    candidate_pool_size: int = 0
    explicit_edge_count: int = 0
    empty_body_refs: tuple[tuple[int, int], ...] = ()
    unresolved_explicit_edges: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()
    """명시 표기가 있는데 후보를 만들 수 없었던 간선.

    출발 또는 도착 조문이 비교 대상 두 버전 어디에도 없는 경우다. 실측: 특금법
    2011↔2020 의 명시 표기 15건 중 3건(`제7조의2→제10조의2`, `제9조의2→제12조의2`,
    `제11조의2→제15조의2`)이 여기 해당한다 — 출발 조문이 2011판에 존재하지 않는다.
    2011년 이후에 신설되고 그 뒤에 이동한 조문이므로, 이 두 버전만 보고는 이동을
    후보로 만들 수 없다.

    **버리지 않고 남기는 이유**: 명시 표기 건수(15)와 EXPLICIT 후보 수(12)가 다른
    것을 설명할 수 있어야 한다. 설명이 없으면 다음 사람이 파서가 3건을 놓쳤다고
    의심하게 되고, 실제로 그런 사고가 있었다(정규식이 조사를 놓쳐 53건 누락, 사건 3).
    """


@dataclass(frozen=True, slots=True)
class DiffCounts:
    """변경 유형별 건수. 조문 개수 보존이 여기서 단언된다."""

    from_article_count: int = 0
    to_article_count: int = 0
    added: int = 0
    deleted: int = 0
    modified: int = 0
    editorial: int = 0
    unchanged: int = 0

    def verify(self, *, context: str) -> None:
        """조문 개수 보존을 양방향으로 검사한다.

        목적:
            "세지 않은 조문"이 생기는 경로를 막는다.

        구현 이유:
            한쪽만 검사하면 반대쪽 누락이 통과한다. `from` 기준만 보면 ADDED 의
            누락을, `to` 기준만 보면 DELETED 의 누락을 놓친다.

            `assert` 를 쓰지 않는다. `python -O` 에서 사라지기 때문이다.

        트레이드오프:
            정상 경로에서도 매번 돈다. 덧셈 두 번이므로 비용이 없다.

        엣지 케이스:
            - 이 단언이 깨진 사고가 실제로 있었다. 집계 스크립트의 dict 붕괴로
              `0013001` 이 소실돼 "벌칙 1:2"로 잘못 세었고, 그 숫자가 ADR-003 의
              근거로 쓰였다 (silent-undercounting.md 사건 1). 실제로는 2:2 다.
        """
        left = self.deleted + self.modified + self.editorial + self.unchanged
        right = self.added + self.modified + self.editorial + self.unchanged
        if left != self.from_article_count or right != self.to_article_count:
            raise DiffError(
                f"조문 개수 보존 단언 실패 ({context}): "
                f"from {self.from_article_count} vs 삭제{self.deleted}+수정{self.modified}"
                f"+문구{self.editorial}+무변경{self.unchanged}={left} / "
                f"to {self.to_article_count} vs 신설{self.added}+수정{self.modified}"
                f"+문구{self.editorial}+무변경{self.unchanged}={right}"
            )


class DiffError(RuntimeError):
    """diff 계산의 실패. 조용한 기본값 대신 호출자에게 알린다."""


@dataclass(frozen=True, slots=True)
class DiffResult:
    """두 버전 비교 결과 전체."""

    counts: DiffCounts
    changes: tuple[ArticleChange, ...] = ()
    candidates: tuple[MoveCandidate, ...] = ()
    moves_in_window: int = 0
    moves_out_of_window: int = 0
    out_of_window_dates: tuple[str, ...] = ()
    candidate_pool_size: int = 0
    empty_body_refs: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    """본문이 비어 유사도 후보에서 제외한 조문. 조용히 0점 처리하지 않는다 (ADR-003)."""

    explicit_edge_count: int = 0
    """창 안 명시 간선 수. EXPLICIT 후보 수와 다를 수 있다 — 아래 참조."""

    unresolved_explicit_edges: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = field(
        default_factory=tuple
    )
    """명시 표기가 있는데 후보를 만들 수 없었던 간선. `MoveMetrics` 의 같은 필드 참조."""
