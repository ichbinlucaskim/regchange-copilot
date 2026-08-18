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
    ministry_code: str | None
    """`<소관부처 소관부처코드="1160100">`의 **XML 속성**이다.

    태그 텍스트만 읽으면 이 값을 통째로 놓친다 — 조문키가 `<조문단위>`의 속성이라
    태그만 순회하는 파서가 놓쳤던 것(ADR-001)과 같은 함정이다. 부서 배정은 이름이
    아니라 코드를 참조하므로(ADR-009), 이 값이 없으면 소관부처를 해결할 수 없다.
    본문 픽스처 13개 전부에서 관측된다.
    """

    revision_kind: str | None
    """`<제개정구분>` — 제정/일부개정/전부개정/타법개정 등.

    **`법종구분`(법률/대통령령)과 다른 축이다.** 12개월 전수에서 조문 이벤트의
    56.2%가 타법개정인데 변경이력 API의 `변경사유`로는 식별되지 않는다 —
    타법개정 조문 20,070건의 변경사유가 전부 `조문변경`이다. 이 값을 따로 봐야
    타법개정을 가릴 수 있다.

    검색 API의 필드명은 `제개정구분명`이고 본문 API는 `제개정구분`이다. 같은 값을
    가리키지만 태그 이름이 다르므로 계열별로 확인해야 한다.
    """

    promulgation_date: dt.date | None
    promulgation_no: str | None
    """`<공포번호>`. 공포일자와 함께 하나의 공포를 식별한다.

    공포일자만으로는 부족하다 — 12개월 전수에서 **같은 날 같은 법령이 두 번 공포된
    사례가 48건** 관측됐다(edge-case #18, 47개 그룹 전부 MST가 다르다).

    **한계**: 이 값을 저장해도 이동 표기의 날짜 창을 더 정밀하게 만들지는 못한다.
    `조문참고자료`의 이동 표기에는 날짜만 있고 공포번호가 없으므로, 같은 날 두 공포 중
    어느 쪽 표기인지는 여전히 구별할 수 없다. 저장했으니 해결됐다고 오해하지 않는다.
    """

    document_effective_date: dt.date | None
    article_effective_dates_raw: str | None
    """`<기본정보><조문시행일자문자열>` **원문 그대로**. 해석하지 않는다.

    `20251216:제42조제3항,제42조의2제1항` 형태로, **조문별 시행일 분기를 요약 문자열로**
    담는다. 본문 픽스처 13개 중 8개가 이 필드를 가지며 8개 전부 값이 있다.

    같은 문서의 조문 단위 `<조문시행일자>` 태그는 문서 시행일자로 평탄화되어 있다
    (edge-case #8). 즉 **분기 정보는 본문 API 에 있으나 조문 단위 필드에는 없다.**

    **파싱하지 않는 이유**: (1) 입도가 조문 단위보다 잘다 — `제42조제3항`,
    `제10조의12제3항제8호`, `제50조제1항 단서`. 항·호·단서를 조문 단위 모델에 어떻게
    매핑할지가 미결정이다. (2) 자유 텍스트라 파싱하면 ADR-007 의 `PARSED` 등급이며,
    그 값을 bitemporal 의 핵심 축(`valid_from`)에 넣을 수는 없다. (3) 이동 표기
    정규식이 조사 하나를 놓쳐 53건을 누락한 전례가 있다 (사건 3).

    **파싱하지 않아도 하나는 할 수 있다** — `has_article_level_effective_dates` 참조.
    """

    units: tuple[ArticleUnit, ...]

    @property
    def has_article_level_effective_dates(self) -> bool:
        """이 문서에 조문별 시행일 분기가 있는가.

        요약 문자열을 파싱하지 않아도 **존재 여부는 알 수 있다.** 이력 API 를 결합할 때
        이 플래그가 켜진 문서에서 이력이 분기를 주지 않으면, 그 자체가 조사 신호다.
        대조 근거를 확보해 두는 것이 이 필드를 저장하는 실질적 이유다.
        """
        return bool(self.article_effective_dates_raw)

    @property
    def articles(self) -> tuple[ArticleUnit, ...]:
        """조문 본체만. **인용 대상이 될 수 있는 것은 이것뿐이다** (ADR-001)."""
        return tuple(u for u in self.units if u.unit_type is UnitType.ARTICLE)
