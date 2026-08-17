"""법제처 응답의 형태를 분류한다. HTTP 상태코드로는 실패를 구별할 수 없다.

목적:
    응답 바이트를 받아 `ResponseKind` 6종 중 하나로 분류하고, `OK`인 경우에만
    파싱된 트리와 건수를 담은 값을 반환한다. **`OK` 외 전부 실패다.**

구현 이유:
    모든 응답이 HTTP 200이고 실패는 본문 형태로만 구별된다 (edge-case #10).
    `raise_for_status()`는 아무것도 잡지 못한다.

    **분기 기준을 target이 아니라 계열(family)로 잡았다.** 보강 단계의 실측에서
    메타 필드 유무가 개별 API의 결함이 아니라 계열의 성질임이 드러났다
    (`law-api-spec.md` §2.1):

    | 계열 | totalCnt | page | numOfRows | resultCode |
    |---|---|---|---|---|
    | 검색 | O | O | O | O |
    | 이력 | O | X | X | X |
    | 본문 | X | X | X | X |

    target을 하나씩 나열하는 방식은 확장되지 않는다 — 앞으로 추가될 이력 API도
    같은 성질을 가질 것이므로, 새 target을 등록할 때 계열만 지정하면 검사 규칙이
    따라오게 했다.

    **성공과 실패를 다른 타입으로 반환한다.** 실패를 `None`이나 빈 리스트로
    반환하면 호출자가 0건과 섞는다. R-11이 정확히 그 혼동이며, 이 저장소의
    사건 기록 3건이 전부 "누락이 정상으로 보였다"는 형태였다.

트레이드오프:
    `PARSE_ERROR`의 의미를 **XML 비적격 + 계열 필수 구조 결여**까지 넓혔다.
    분류를 6종으로 고정했으므로 "루트는 맞는데 `totalCnt`가 없다"를 담을 칸이
    따로 없다. 종수를 늘리는 대신 `detail`에 사유를 적는 쪽을 택했다 — 종이
    늘면 호출부의 분기가 늘고, 어느 종이 실패인지 기억해야 할 것이 많아진다.
    `OK` 하나만 성공이라는 규칙이 종수보다 중요하다.

    분류는 **형태만 본다. 0건 여부를 판단하지 않는다.** 0건이 정상인지 실패인지는
    이력 계열에서 원리적으로 구별 불가능하며(아래), 그 판단에는 재요청과 카나리아가
    필요하다. 형태 분류에 섞으면 순수 함수가 네트워크에 의존하게 된다.

엣지 케이스:
    - **이력 계열에서 정상 0건과 파라미터 오류는 구별 불가능하다.** 아래
      `ResponseKind` docstring과 `HISTORY_ZERO_IS_AMBIGUOUS`에 근거를 적었다.
    - 인코딩 선언이 `UTF-8`/`utf-8`로 API마다 갈리지만 실제 바이트는 모두
      UTF-8이다 (edge-case #15). **선언을 읽지 않고 UTF-8로 고정 해석한다.**
      선언 문자열을 로직이나 캐시 키에 쓰지 않는다.
    - **마스킹을 파싱보다 먼저 한다.** 응답의 `조문링크`에 OC가 echo되므로
      (edge-case #1), 파싱 후에 마스킹하면 트리 안에 원본 자격증명이 남는다.
      마스킹 실패는 분류 결과가 아니라 `MaskingError` 예외로 전파된다 — 저장·
      로깅 이전에 중단시켜야 하므로 "실패의 한 종류"로 격하하지 않는다.
    - `<Law>` 메시지 응답을 `ROOT_MISMATCH`보다 먼저 판정한다. `<Law>`는 어떤
      spec에서도 기대 루트가 아니므로 둘 다 참이지만, `LAW_MESSAGE`가 더 구체적인
      진단(파라미터가 틀렸다)을 준다.
    - 0바이트 응답이 정상이다 — 알 수 없는 target에 대해 HTTP 200 + 본문 0바이트가
      온다 (`error_unknown_target_empty.xml`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError as XmlParseError

from defusedxml.ElementTree import fromstring as _safe_fromstring

from regchange.ingest.masking import Masker

BODY_EXCERPT_LIMIT = 500
"""실패 로그에 남기는 본문 발췌 길이. 형태를 판별할 만큼은 남기되 전문을 로그에
쏟지 않는다. HTML 안내 페이지가 1,649바이트이므로 500자면 어떤 페이지인지 드러난다."""

_HTML_SNIFF_LIMIT = 200
"""HTML 판별에 볼 선행 바이트 수. `error_target_lsHistory.html`의 DOCTYPE과
`<html` 태그가 120바이트 안에 있다."""


class ResponseFamily(StrEnum):
    """target의 계열. 메타 필드 유무와 완주 검사 대상을 결정한다.

    목적:
        `law-api-spec.md` §2.1에서 실측된 세 계열을 타입으로 고정한다.

    구현 이유:
        계열이 곧 검사 규칙이다. `SEARCH`/`HISTORY`는 `totalCnt`가 있어 완주
        검사 대상이고, `DOCUMENT`은 없어서 대상이 아니다. 이 구별을 호출부의
        `if target == ...`로 두면 새 target마다 분기가 늘어난다.

    트레이드오프:
        계열이 셋뿐이라 과한 추상화로 보일 수 있다. 그러나 이 셋은 우리가 나눈
        것이 아니라 **API가 실제로 그렇게 동작하는** 구분이며(픽스처 28개 실측),
        나누지 않으면 `totalCnt`가 없는 본문 응답에 완주 검사를 걸어 항상
        실패하게 된다.

    엣지 케이스:
        - `admrul` 계열도 같은 세 계열에 들어가지만 이 작업 범위 밖이다
          (ADR-006, 3단계). 등록만 하지 않았을 뿐 계열 구분은 그대로 적용된다.
    """

    SEARCH = "SEARCH"
    """lawSearch.do의 목록 검색. `resultCode`가 있는 유일한 계열이다."""

    HISTORY = "HISTORY"
    """변경이력. `totalCnt`는 있으나 `resultCode`·`page`·`numOfRows`가 없다."""

    DOCUMENT = "DOCUMENT"
    """lawService.do의 본문. 메타 필드가 전혀 없고 완주 검사 대상이 아니다."""


class ResponseKind(StrEnum):
    """응답 형태 분류 — `OK` 외 전부 실패다.

    목적:
        "실패했다"와 "0건이다"를 절대 같은 값으로 표현하지 않기 위한 분류축.

    구현 이유:
        edge-case #10대로 HTTP 상태코드가 성공/실패를 구별하지 않으므로, 본문
        형태를 우리가 분류해야 한다. 종을 6개로 고정한 이유는 각각이 **다른
        운영 조치**로 이어지기 때문이다 — 재시도 대상인지, 파라미터를 고쳐야
        하는지, 활용신청이 필요한지가 종마다 다르다.

    트레이드오프:
        `PARSE_ERROR`가 두 사유(XML 비적격 / 계열 필수 구조 결여)를 겸한다.
        모듈 docstring의 트레이드오프 절 참조.

    엣지 케이스:
        **이력 계열의 `totalCnt=0`은 이 분류로 성공/실패를 가릴 수 없다.**
        `error_dayjochg_id_only_zero.xml`(파라미터 부족)과 정상 0건 응답이
        바이트 단위로 동일하다 — 둘 다 아래 형태다.

            <LawSearch><target>lsJoHstInf</target><totalCnt>0</totalCnt></LawSearch>

        `error_search_zero_result.xml`(검색 계열)에는 `resultCode=00`이 있어
        구별되는 것처럼 보이지만, **그 차이는 계열 차이지 성공/실패 차이가
        아니다.** 검색 계열의 0건과 이력 계열의 0건을 비교한 것일 뿐이다.
        이력 계열 안에서는 구별할 수단이 응답에 존재하지 않는다.

        → **카나리아가 유일한 방어선이다** (`HISTORY_ZERO_IS_AMBIGUOUS`,
        ADR-005 R-11). 억지로 구별하려 들지 않는다.
    """

    OK = "OK"
    """기대한 계열 구조를 만족한다. 0건일 수도 있다 — 이 값은 형태만 말한다."""

    EMPTY_BODY = "EMPTY_BODY"
    """본문 0바이트. 알 수 없는 target. 재시도해도 같다."""

    HTML = "HTML"
    """XML 대신 HTML 안내 페이지. 미신청 target. 활용신청이 필요하다."""

    LAW_MESSAGE = "LAW_MESSAGE"
    """`<Law>일치하는 …이 없습니다</Law>`. 파라미터가 틀렸다. 재시도해도 같다."""

    PARSE_ERROR = "PARSE_ERROR"
    """UTF-8 디코딩 실패, XML 비적격, 또는 계열 필수 구조 결여."""

    ROOT_MISMATCH = "ROOT_MISMATCH"
    """루트 태그가 §2.1의 기대값과 다르다. endpoint나 target을 잘못 골랐다."""


HISTORY_ZERO_IS_AMBIGUOUS: Final = True
"""이력 계열에서 정상 0건과 요청 실패를 응답만으로 구별할 수 없다는 사실.

상수로 둔 이유는 이것이 **구현의 한계가 아니라 API의 성질**이며, 코드를 고쳐
해결할 수 없다는 것을 코드 안에 남기기 위해서다. 이 값이 `True`인 동안
0건 판정은 응답 형태가 아니라 **카나리아와 0건 재요청**이 담당한다.

근거: ADR-005 R-11, `error_dayjochg_id_only_zero.xml`, `law-api-spec.md` §2.1.
`False`로 바꿀 수 있는 조건은 법제처가 이력 계열에 `resultCode`를 추가하는 것뿐이며,
그때는 이 상수를 지우고 분류 로직에 검사를 넣는다.
"""


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """target 하나의 요청 규격과 기대 응답 구조 (`law-api-spec.md` §2.1).

    목적:
        루트 태그 대조(`ROOT_MISMATCH`)와 완주 검사 대상을 한 곳에 모은다.

    구현 이유:
        표를 코드에 두는 이유는 §2.1이 **실측 결과**이고 추측이 섞이면 즉시
        틀리기 때문이다. `item_path`와 `article_path`를 나눈 것이 핵심이다 —
        같은 `lsJoHstInf`가 endpoint에 따라 조문의 위치가 다르다.

    트레이드오프:
        스펙 문서와 코드에 같은 표가 두 벌 존재해 어긋날 수 있다. 문서를 읽어
        코드를 생성하는 방식을 택하지 않은 대신, 각 필드에 근거 픽스처를 주석으로
        붙여 대조 가능하게 했다. 픽스처 기반 테스트가 두 벌의 일치를 검사한다.

    엣지 케이스:
        - `total_count_expected`가 False인 계열(`DOCUMENT`)에 완주 검사를 걸면
          항상 실패한다. `family`에서 파생되므로 개별 지정 실수가 생기지 않는다.
        - `article_path`가 None인 spec(검색·본문)에서 조문 수를 묻지 않는다.
        - **루트 태그만으로는 target을 구별할 수 없다.** `law`/`eflaw` 검색,
          `lsHstInf`, `lsJoHstInf`(일자별)이 **전부 루트 `LawSearch` + 항목
          `law`**다. 그런데 항목 내부 구조는 다르다 — 검색과 `lsHstInf`는
          `법령일련번호`가 `<law>` 직계인데 `lsJoHstInf`는 `법령정보` 아래에
          중첩된다. 그래서 `identity_path`를 spec마다 따로 두고, 응답이 echo하는
          `<target>`을 대조한다 (`target_echoed`).
    """

    key: str
    """코드에서 쓰는 식별자. 로그와 메타데이터에 이 값이 남는다."""

    target: str
    """API의 `target` 파라미터 값. 응답의 `<target>`과 대조한다."""

    endpoint: str
    """`lawSearch.do` 또는 `lawService.do`. 같은 target이 endpoint로 갈린다."""

    family: ResponseFamily
    """계열. 메타 필드 유무와 완주 검사 대상을 결정한다."""

    root_tag: str
    """기대 루트 태그. 다르면 `ROOT_MISMATCH`."""

    item_path: str
    """**완주 검사 대상** 요소의 상대 경로. 이력 계열에서는 법령 요소다."""

    required_params: frozenset[str]
    """이 target이 요구하는 파라미터. 요청 전 검증에 쓴다."""

    optional_params: frozenset[str] = frozenset()
    """이 target이 받는 **의미 파라미터** 중 선택적인 것.

    `required_params`와 합쳐 **허용 목록**이 된다 (`semantic_params`). 이 목록에
    없는 키는 스냅샷 매니페스트에 기록되지 않고 예외가 난다 — **거부 목록보다
    허용 목록이 안전하다.** 전체 쿼리를 남기면 나중에 추가되는 파라미터가 자동으로
    따라 들어가고, 그중에 민감한 것이 섞이면 마스킹 목록을 갱신하지 않는 한
    조용히 저장된다.

    `display`·`page`는 여기 넣지 않는다 — 페이지네이션은 **같은 요청의 수행
    방식**이지 요청 자체가 아니다. `OC`·`type`도 넣지 않는다(자격증명과 상수)."""

    article_path: str | None = None
    """조문 요소의 경로. 이력 계열에만 있고 완주 검사 대상이 **아니다**."""

    identity_path: str | None = None
    """항목 안에서 **버전 식별자**(법령일련번호/MST)의 경로. 중복 판정의 키다.

    target마다 다르다 — 검색·`lsHstInf`는 `법령일련번호`, `lsJoHstInf`는
    `법령정보/법령일련번호`다. 루트 태그가 같아도 이 경로가 다르므로 spec마다
    따로 둔다 (실측: `law-api-spec.md` §2.1)."""

    law_id_path: str | None = None
    """항목 안에서 **법령 자체의 식별자**(법령ID)의 경로.

    `identity_path`와 나눈 이유가 핵심이다 — 같은 `법령ID`에 다른 MST는 **연혁이며
    정상 데이터**다. 법령ID로 중복 제거하면 연혁이 통째로 사라진다 (edge-case #18).

    조문별 이력에서는 None이다 — 그 응답은 법령ID로 조회하므로 값이 응답 루트에
    한 번 있고 항목에는 없다 (실측: `jochg_009244_jo000200_full.xml`)."""

    article_no_path: str | None = None
    """조문 요소 안에서 조문번호의 경로. 이력 계열의 식별키 구성 요소다."""

    version_time_path: str | None = None
    """조문 요소 안에서 **시점 필드**의 경로. **계열마다 다른 필드를 쓴다.**

    이 필드가 식별키의 결정적 구성 요소다 (edge-case #18):

    | 계열 | 시점 필드 | 근거 |
    |---|---|---|
    | 일자별 (lawSearch.do) | `조문시행일` | 캐시 365일 35,681행 충돌 0건 |
    | 조문별 (lawService.do) | `조문변경일` | 픽스처 3종 56행 충돌 0건 |

    **두 계열이 다른 필드를 쓰는 이유는 스키마가 애초에 다르기 때문이다** (§2.1) —
    일자별 `<jo>`에는 `조문개정일`·`조문시행일`이 있고, 조문별 `<조문정보>`에는
    `조문변경일`이 있다. 하나의 보편 키를 만들려 하면 어느 한쪽에서 필드가 없어
    무너진다.

    **이 필드를 키에서 빼면 실제 데이터가 지워진다.** 일자별에서 `조문시행일`을
    빼면 35,681행 중 **439건이 충돌**하는데, 그 439건은 같은 공포(MST)가 조문을
    여러 시점에 나누어 시행한 기록이다. 그것이 `valid_from`이며 ADR-005가 이력
    API를 진입점으로 삼은 이유 자체다."""

    @property
    def total_count_expected(self) -> bool:
        """`totalCnt`가 응답에 있어야 하는 계열인가. `DOCUMENT`만 False다."""
        return self.family is not ResponseFamily.DOCUMENT

    @property
    def semantic_params(self) -> frozenset[str]:
        """요청을 논리적으로 식별하는 파라미터의 **허용 목록**.

        이 집합이 곧 **"무엇이 같은 요청인가"의 정의**다. 스냅샷 디렉터리 키와
        매니페스트 `params`가 모두 이 목록만 본다. `display`가 여기 없으므로
        `display=50`과 `display=100`은 같은 요청이며, 같은 디렉터리로 간다 —
        논리적으로 같은 것을 다르게 받은 것뿐이다.
        """
        return self.required_params | self.optional_params

    @property
    def target_echoed(self) -> bool:
        """응답이 `<target>`을 echo하는 계열인가. `DOCUMENT`만 False다.

        본문 응답의 루트는 `<법령>`이고 `<target>` 요소가 없다 (실측 13종).
        검색·이력 계열은 전부 echo하므로 대조에 쓸 수 있다.
        """
        return self.family is not ResponseFamily.DOCUMENT


LAW_SEARCH: Final = TargetSpec(
    key="law_search",
    target="law",
    endpoint="lawSearch.do",
    family=ResponseFamily.SEARCH,
    root_tag="LawSearch",
    item_path="law",
    required_params=frozenset({"query"}),
    optional_params=frozenset({"section"}),
    identity_path="법령일련번호",
    law_id_path="법령ID",
)
"""법령 목록 검색. 근거: `search_law_teukgeum.xml`.

주의: 법령명 검색은 예상 밖 결과를 반환한다 — `query=은행법`이 상호저축은행법을
먼저 반환한다 (edge-case #19). **감시 대상은 이름이 아니라 법령ID로 고정한다**
(`config/corpus.yaml`). 이 spec은 탐색·확인용이며 감시 대상 선정에 쓰지 않는다."""

EFLAW_SEARCH: Final = TargetSpec(
    key="eflaw_search",
    target="eflaw",
    endpoint="lawSearch.do",
    family=ResponseFamily.SEARCH,
    root_tag="LawSearch",
    item_path="law",
    required_params=frozenset({"query"}),
    optional_params=frozenset({"section"}),
    identity_path="법령일련번호",
    law_id_path="법령ID",
)
"""시행일 법령 목록 검색. 근거: `search_eflaw_teukgeum.xml`.

**`LAW_SEARCH`와 루트 태그·항목 경로가 완전히 같다.** 구별되는 것은 응답이
echo하는 `<target>`뿐이다 — 루트 대조만으로는 두 target을 가릴 수 없다.

이 픽스처는 `totalCnt=81`인데 10건만 담긴 잘린 응답이다."""

LS_HISTORY: Final = TargetSpec(
    key="ls_history",
    target="lsHstInf",
    endpoint="lawSearch.do",
    family=ResponseFamily.HISTORY,
    root_tag="LawSearch",
    item_path="law",
    required_params=frozenset({"regDt"}),
    article_path=None,
    identity_path="법령일련번호",
    law_id_path="법령ID",
)
"""법령 변경이력 목록. 근거: `lschg_regdt20240719.xml`.

**수집 진입점이 아니다** (ADR-005 근거 3) — 이 target의 `regDt`는 공포일자가
아니라 법제처 DB 등록일로 보이며 **미확인**이다. 그래도 등록하는 이유는 공포일과
등록일의 차이가 **우리가 통제할 수 없는 인지 지연의 하한선**이고, 그것을 측정하지
않으면 법제처의 등록 지연이 우리 KPI로 잘못 계상되기 때문이다.

**조문 수준 정보가 없어 `article_path`가 None이다** — 이력 계열이지만 조문이 없는
유일한 spec이다. 근거 픽스처가 `totalCnt=71`인데 5건만 담긴 잘린 응답이며, 5건은
법령명 가나다순 앞부분이라 표본이 편향돼 있다 (미수 1)."""

LAW_DOCUMENT: Final = TargetSpec(
    key="law_document",
    target="law",
    endpoint="lawService.do",
    family=ResponseFamily.DOCUMENT,
    root_tag="법령",
    item_path="조문/조문단위",
    required_params=frozenset({"MST"}),
)
"""법령 본문. 근거: `law_*.xml` 9종. `MST`는 법령일련번호(버전 식별자)다."""

EFLAW_DOCUMENT: Final = TargetSpec(
    key="eflaw_document",
    target="eflaw",
    endpoint="lawService.do",
    family=ResponseFamily.DOCUMENT,
    root_tag="법령",
    item_path="조문/조문단위",
    required_params=frozenset({"MST", "efYd"}),
)
"""시행일 법령 본문. 근거: `eflaw_*.xml` 4종. 같은 MST가 `efYd`마다 다른 응답을 낸다."""

JO_HISTORY_BY_DATE: Final = TargetSpec(
    key="jo_history_by_date",
    target="lsJoHstInf",
    endpoint="lawSearch.do",
    family=ResponseFamily.HISTORY,
    root_tag="LawSearch",
    item_path="law",
    required_params=frozenset({"regDt"}),
    article_path="law/조문정보/jo",
    identity_path="법령정보/법령일련번호",
    law_id_path="법령정보/법령ID",
    article_no_path="조문번호",
    version_time_path="조문시행일",
)
"""일자별 조문 개정 이력. 근거: `dayjochg_*.xml` 7종.

`regDt`는 **공포일자**다 (ADR-005). 시행일이 아니다.
`totalCnt`는 **법령 수**이지 조문 수가 아니다 — `dayjochg_regdt20250401.xml`은
`totalCnt=83`인데 조문은 286건이다."""

JO_HISTORY_BY_ARTICLE: Final = TargetSpec(
    key="jo_history_by_article",
    target="lsJoHstInf",
    endpoint="lawService.do",
    family=ResponseFamily.HISTORY,
    root_tag="LawService",
    item_path="law",
    required_params=frozenset({"ID", "JO"}),
    article_path="law/조문정보",
    identity_path="법령정보/법령일련번호",
    law_id_path=None,
    article_no_path="조문번호",
    version_time_path="조문변경일",
)
"""조문별 변경 이력. 근거: `jochg_*.xml` 3종.

**`ID`는 법령ID다 (MST 아님).** `JO`는 6자리 = 조문번호4 + 가지번호2.

**루트 태그가 `LawService`이고 조문 경로에 `jo` 래퍼가 없다** — 같은 target인
일자별(`JO_HISTORY_BY_DATE`)과 다르다. 이 차이를 무시하면 조문이 조용히
0건이 된다 (`test_by_article_response_under_by_date_path_yields_zero`)."""

SPECS: Final = (
    LAW_SEARCH,
    LAW_DOCUMENT,
    EFLAW_DOCUMENT,
    JO_HISTORY_BY_DATE,
    JO_HISTORY_BY_ARTICLE,
)
"""이 작업 범위의 target 5종. 행정규칙은 ADR-006에 따라 3단계에서 추가한다."""


@dataclass(frozen=True, slots=True)
class ClassifiedOk:
    """형태 검사를 통과한 응답 — 0건일 수도 있다.

    목적:
        파싱된 트리와 계열별 건수를 담아, 완주 검사와 병합이 쓸 수 있게 한다.

    구현 이유:
        실패 타입과 분리했다. 하나의 타입에 `kind`와 `Optional` 필드를 두면
        호출부가 `kind`를 검사하지 않고 필드를 읽을 수 있고, 그때 `None`이
        0건으로 해석된다 — 이 저장소가 세 번 겪은 실패 형태다.

    트레이드오프:
        `Element`를 그대로 들고 있어 pydantic 검증이나 직렬화가 되지 않는다.
        도메인 모델로 변환하는 단계를 뒤로 미룬 대신, 원문 트리를 보존해
        "시스템이 본 원문"을 감사에 제시할 수 있게 했다 (`ingest/__init__.py`).

    엣지 케이스:
        - `total_count`가 None인 것은 **본문 계열**이라는 뜻이며 실패가 아니다.
          완주 검사를 건너뛰어야 한다는 신호다.
        - `body`는 마스킹된 문자열이다. 저장·로깅에 이 값만 쓴다.
    """

    spec: TargetSpec
    root: Element
    body: str
    """마스킹된 응답 본문. 저장·로깅에 쓸 수 있는 유일한 문자열이다."""

    total_count: int | None
    """`totalCnt`. 본문 계열에서는 None이다. 이력 계열에서는 **법령 수**다."""

    items: tuple[Element, ...]
    """`spec.item_path`의 요소들. 완주 검사 대상이다."""

    articles: tuple[Element, ...]
    """`spec.article_path`의 요소들. 완주 검사 대상이 **아니다**. 없으면 빈 튜플."""

    @property
    def kind(self) -> ResponseKind:
        """항상 `OK`. 실패 타입과 같은 이름으로 접근하기 위한 것이다."""
        return ResponseKind.OK


@dataclass(frozen=True, slots=True)
class ClassifiedFailure:
    """형태 검사를 통과하지 못한 응답. `kind`는 절대 `OK`가 아니다.

    목적:
        실패 사유와 진단에 필요한 발췌를 담는다.

    구현 이유:
        성공 타입과 필드가 다르다. 공통 필드를 최소화해 호출부가 실패를 성공처럼
        다루기 어렵게 했다 — `items`나 `total_count`가 아예 없으므로 건수를
        읽으려 하면 타입 검사에서 막힌다.

    트레이드오프:
        `isinstance` 분기가 호출부마다 필요하다. 그 비용이 0건과 실패를 섞는
        위험보다 싸다.

    엣지 케이스:
        - `retryable`은 항상 False다. 응답 형태 실패는 파라미터가 틀린 것이므로
          재시도해도 같다. 네트워크 오류는 이 타입에 오지 않고 전송 계층에서
          재시도된다.
        - `body_excerpt`는 마스킹된 뒤 잘린 문자열이다.
    """

    spec: TargetSpec
    kind: ResponseKind
    detail: str
    body_excerpt: str

    @property
    def retryable(self) -> bool:
        """항상 False. 응답 형태 실패는 재시도 대상이 아니다 (모듈 §9 요청 정책)."""
        return False


type Classified = ClassifiedOk | ClassifiedFailure
"""분류 결과. `isinstance(result, ClassifiedOk)`로 좁힌다."""


def _looks_like_html(text: str) -> bool:
    """선행 바이트에 HTML 표지가 있는가. 미신청 target 안내 페이지 판별용."""
    head = text.lstrip()[:_HTML_SNIFF_LIMIT].lower()
    return head.startswith("<!doctype html") or "<html" in head


def classify(body: bytes, spec: TargetSpec, *, masker: Masker) -> Classified:
    """응답 바이트를 형태로 분류한다. `OK` 외 전부 실패다.

    목적:
        외부 응답이 도메인 코드로 들어오는 유일한 관문. 마스킹과 형태 검사를
        여기서 끝내고, 통과한 것만 파싱된 트리로 내보낸다.

    구현 이유:
        **바이트를 받는다.** 문자열을 받으면 디코딩이 이미 끝난 뒤이고, 그때
        누가 어떤 인코딩으로 읽었는지 알 수 없다. 선언이 `UTF-8`/`utf-8`로
        갈리므로(edge-case #15) 선언을 읽지 않고 **UTF-8로 고정 해석**하려면
        디코딩을 이 함수가 해야 한다.

        **마스킹을 파싱보다 먼저 한다.** `조문링크`에 OC가 echo되므로
        (edge-case #1) 파싱 후 마스킹하면 트리에 원본이 남는다. 마스킹된 텍스트를
        파싱하면 트리·본문·발췌가 전부 안전하고, **픽스처(마스킹된 파일)와
        운영 경로가 같은 코드를 타게 된다.**

        분류 순서는 **싼 검사부터, 그리고 더 구체적인 진단부터**다.
        1. UTF-8 디코딩 — 실패하면 그 뒤 어떤 검사도 의미가 없다
        2. 빈 본문 — 0바이트가 정상 응답으로 온다
        3. HTML 표지 — XML 파서에 넣으면 깨지므로 파싱 전에 걸러낸다
        4. XML 파싱
        5. `<Law>` 메시지 — 루트 대조보다 먼저. 어떤 spec에서도 기대 루트가
           아니므로 둘 다 참이지만 이쪽이 구체적인 진단(파라미터 오류)을 준다
        6. 루트 태그 대조 (§2.1)
        7. 계열별 필수 구조 검사

    트레이드오프:
        `MaskingError`를 분류 결과로 흡수하지 않고 예외로 전파한다. 실패의 한
        종류로 두면 호출부가 다른 실패와 함께 세고 넘어갈 수 있다. 자격증명
        유출은 "세고 넘어갈" 사안이 아니다 (`docs/security-notes.md`).

        0건 여부를 판단하지 않는다. 이력 계열에서 그 판단은 응답만으로 불가능하며
        (`HISTORY_ZERO_IS_AMBIGUOUS`), 재요청·카나리아가 담당한다. 형태 분류를
        순수 함수로 유지하는 대신 호출부가 두 단계를 밟아야 한다.

    엣지 케이스:
        - 0바이트 → `EMPTY_BODY`. 공백뿐인 본문도 같게 본다.
        - UTF-8로 디코딩되지 않는 바이트 → `PARSE_ERROR`. 선언을 보고 다른
          인코딩으로 재시도하지 않는다 — 선언을 신뢰하지 않기로 했으므로
          재시도 기준이 없다.
        - 루트는 맞지만 `totalCnt`가 없거나 정수가 아닌 경우 → `PARSE_ERROR`.
          계열 계약 위반이며, 이것을 통과시키면 완주 검사가 0을 기준으로 돌아
          잘림을 놓친다.
        - 본문 계열에 `totalCnt`가 **있는** 경우는 실패로 보지 않는다. 관측되지
          않았고, 있다고 해서 본문이 덜 유효해지지 않는다. 다만 `total_count`는
          None으로 둔다 — 완주 검사 대상이 아니라는 계열의 성질이 우선한다.
        - 조문 0건은 실패로 보지 않는다. 형태가 아니라 내용 판단이며, 수집
          계층이 결정한다 (ADR-005 "조문 0건 수집은 실패").
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.PARSE_ERROR,
            detail=(
                f"UTF-8 디코딩 실패: {exc}. 인코딩 선언을 보고 재시도하지 않는다 "
                "— 선언을 신뢰하지 않기로 했으므로(edge-case #15) 재시도 기준이 없다"
            ),
            body_excerpt=repr(body[:BODY_EXCERPT_LIMIT]),
        )

    masked = masker.mask(text)
    excerpt = masked[:BODY_EXCERPT_LIMIT]

    if not masked.strip():
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.EMPTY_BODY,
            detail=(
                f"본문이 비어 있다({len(body)}바이트). 알 수 없는 target일 때 "
                "HTTP 200과 함께 0바이트가 온다"
            ),
            body_excerpt=excerpt,
        )

    if _looks_like_html(masked):
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.HTML,
            detail=(
                f"XML 대신 HTML이 왔다. target={spec.target!r}이 이 OC로 "
                "미신청 상태일 수 있다. 활용신청이 필요하며 재시도해도 같다"
            ),
            body_excerpt=excerpt,
        )

    try:
        root: Element = _safe_fromstring(masked)
    except XmlParseError as exc:
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.PARSE_ERROR,
            detail=f"XML 파싱 실패: {exc}",
            body_excerpt=excerpt,
        )

    if root.tag == "Law":
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.LAW_MESSAGE,
            detail=(
                f"법제처 안내 메시지: {(root.text or '').strip()!r}. "
                "파라미터가 틀렸다. 재시도해도 같다"
            ),
            body_excerpt=excerpt,
        )

    if root.tag != spec.root_tag:
        return ClassifiedFailure(
            spec=spec,
            kind=ResponseKind.ROOT_MISMATCH,
            detail=(
                f"루트 태그가 <{root.tag}>인데 {spec.key}는 <{spec.root_tag}>를 "
                f"기대한다 (law-api-spec.md §2.1). endpoint({spec.endpoint})나 "
                "target을 잘못 골랐을 수 있다"
            ),
            body_excerpt=excerpt,
        )

    if spec.target_echoed:
        echoed = (root.findtext("target") or "").strip()
        if echoed != spec.target:
            return ClassifiedFailure(
                spec=spec,
                kind=ResponseKind.ROOT_MISMATCH,
                detail=(
                    f"응답이 echo한 target이 {echoed!r}인데 {spec.key}는 "
                    f"{spec.target!r}를 기대한다. **루트 태그만으로는 target을 "
                    "구별할 수 없다** — law/eflaw 검색·lsHstInf·lsJoHstInf(일자별)이 "
                    "전부 루트 <LawSearch> + 항목 <law>이지만 항목 내부 구조가 다르다"
                ),
                body_excerpt=excerpt,
            )

    total_count: int | None = None
    if spec.total_count_expected:
        raw_total = root.findtext("totalCnt")
        if raw_total is None or not raw_total.strip().isdigit():
            return ClassifiedFailure(
                spec=spec,
                kind=ResponseKind.PARSE_ERROR,
                detail=(
                    f"{spec.family.value} 계열은 totalCnt가 있어야 하는데 "
                    f"{raw_total!r}이다. 이것을 통과시키면 완주 검사가 0을 기준으로 "
                    "돌아 잘림을 놓친다"
                ),
                body_excerpt=excerpt,
            )
        total_count = int(raw_total.strip())

    articles = tuple(root.findall(spec.article_path)) if spec.article_path else ()
    return ClassifiedOk(
        spec=spec,
        root=root,
        body=masked,
        total_count=total_count,
        items=tuple(root.findall(spec.item_path)),
        articles=articles,
    )
