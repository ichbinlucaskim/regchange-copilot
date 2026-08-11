"""파서 출력 모델.

목적:
    조문 트리와 정규화 텍스트를 타입이 붙은 값으로 표현한다. diff·적재 계층이
    이 모델만 보고 동작한다.

구현 이유:
    모든 컬렉션을 `tuple`로 둔다. 조문 트리는 파싱이 끝나면 불변이며, 리스트로
    두면 하위 계층이 조용히 변형할 수 있다. 특히 **순서가 의미를 갖는 구조**라
    (목의 호 귀속이 문서 순서로만 결정된다) 사후 정렬·재배치를 막아야 한다.

트레이드오프:
    조립 중에는 리스트를 쓰고 마지막에 tuple로 굳혀야 해 파서 코드가 조금 길어진다.
    그 대신 모델을 받은 쪽에서 순서를 깨뜨릴 수 없다.

엣지 케이스:
    - `Hang.num`이 None일 수 있다. 항 구분 없이 호만 있는 조문에 빈 껍데기 항이
      삽입되기 때문이다 (4개 법령 합계 51건).
    - `ArticleUnit.title`이 None일 수 있다. `조문제목`은 조건부 태그다
      (특금법 34개 중 27개만 보유).
    - `article_key`는 유일하지 않다. `(document_key, article_key, seq_in_doc)`이
      유일하다 (ADR-001).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class UnitType(StrEnum):
    """조문키 유형 자리(7번째)의 의미. `0`=제목행, `1`=조문 본체 (ADR-001)."""

    ARTICLE = "ARTICLE"
    HEADING = "HEADING"


class MarkerType(StrEnum):
    """꺾쇠 마커의 종류. 판별 기준은 키워드가 아니라 **날짜 형태 포함 여부**다."""

    AMENDED = "개정"
    INSERTED = "신설"
    UNKNOWN_DATE = "미상"
    """키워드 없이 날짜만 있는 마커. `② 삭제 <2013.8.13>` 형태로 나타난다."""
    PROVISO_OMITTED = "단서생략"
    """`<단서 생략>`. 날짜가 없어 제거 대상이 아니며 추출만 한다."""


class Frozen(BaseModel):
    """불변 모델 공통 설정."""

    model_config = ConfigDict(frozen=True)


class AmendmentMarker(Frozen):
    """본문 텍스트에 포함된 꺾쇠 개정 마커 하나."""

    type: MarkerType
    dates: tuple[dt.date, ...] = ()
    raw: str


class NormalizedText(Frozen):
    """원문과 정규화본을 함께 담는다 (ADR-002)."""

    raw: str
    norm: str
    sha256: str
    rule_version: str
    markers: tuple[AmendmentMarker, ...] = ()


class ArticleRef(Frozen):
    """조문 참조 하나. `제5조의2` → `(5, 2)`."""

    article_no: int
    branch_no: int = 0

    @property
    def jo(self) -> str:
        """`JO` 파라미터 형식(6자리). 조문번호 4 + 가지번호 2 (spec §3.2)."""
        return f"{self.article_no:04d}{self.branch_no:02d}"

    def render(self) -> str:
        """사람이 읽는 표기. 저장 값이 아니라 렌더링 결과다 (ADR-001)."""
        return f"제{self.article_no}조" + (f"의{self.branch_no}" if self.branch_no else "")


class MoveKind(StrEnum):
    """`조문참고자료`의 이동 표기 종류."""

    MOVED_FROM = "MOVED_FROM"
    """이 조문이 종전 어느 조문에서 왔는가. `제6조에서 이동`"""
    PREVIOUS_MOVED_TO = "PREVIOUS_MOVED_TO"
    """이 번호를 쓰던 종전 조문이 어디로 갔는가. `종전 제9조는 제12조로 이동`"""
    PREVIOUS_DELETED = "PREVIOUS_DELETED"
    """이 번호를 쓰던 종전 조문이 삭제됐다. `종전 제34조의2는 삭제`"""


class MoveReference(Frozen):
    """`조문참고자료`에서 추출한 이동 표기.

    이것은 API가 구조화해 준 필드가 아니라 **우리가 텍스트에서 추출한 것**이다.
    따라서 ADR-007의 `PARSED`와 같은 지위이며, 신뢰도가 100%가 아니다 (ADR-003).
    `raw`를 반드시 보존해 사후 검증이 가능하게 한다.
    """

    kind: MoveKind
    source: ArticleRef | None = None
    target: ArticleRef | None = None
    dates: tuple[dt.date, ...] = ()
    raw: str


class Mok(Frozen):
    """목(가.나.다.). 문서 순서로 직전 `Ho`에 귀속된다 (edge-case #4)."""

    num: str
    text: NormalizedText


class Ho(Frozen):
    """호(1.2.3.)와 그에 귀속된 목들."""

    num: str
    text: NormalizedText
    moks: tuple[Mok, ...] = ()


class Hang(Frozen):
    """항(①②③). `num`이 None이면 호만 담는 빈 껍데기 항이다."""

    num: str | None
    text: NormalizedText
    hos: tuple[Ho, ...] = ()


class ArticleUnit(Frozen):
    """`<조문단위>` 하나. 조문 본체이거나 편장절관 제목행이다."""

    article_key: str
    article_no: int
    branch_no: int
    unit_type: UnitType
    seq_in_doc: int
    title: str | None
    content: NormalizedText
    hangs: tuple[Hang, ...] = ()
    heading_path: tuple[str, ...] = ()
    effective_date: dt.date | None = None
    changed: bool = False
    reference_raw: str | None = None
    moves: tuple[MoveReference, ...] = ()

    @property
    def ref(self) -> ArticleRef:
        """이 조문의 참조값."""
        return ArticleRef(article_no=self.article_no, branch_no=self.branch_no)

    def reconstructed_key(self) -> str:
        """`(article_no, branch_no, unit_type)`으로 조문키를 재구성한다.

        채점용이다. 원본 `@조문키`와 일치해야 한다 — 4개 법령 1,189조문에서
        불일치 0건이 0.7단계에 확인됐다 (spec §4.2).
        """
        type_digit = "1" if self.unit_type is UnitType.ARTICLE else "0"
        return f"{self.article_no:04d}{self.branch_no:02d}{type_digit}"


class LawDocument(Frozen):
    """법령 본문 응답 하나(= 하나의 MST 버전)."""

    law_id: str
    law_name: str
    law_kind: str | None
    ministry: str | None
    promulgation_date: dt.date | None
    document_effective_date: dt.date | None
    units: tuple[ArticleUnit, ...]

    @property
    def articles(self) -> tuple[ArticleUnit, ...]:
        """조문 본체만. **인용 대상이 될 수 있는 것은 이것뿐이다** (ADR-001)."""
        return tuple(u for u in self.units if u.unit_type is UnitType.ARTICLE)
