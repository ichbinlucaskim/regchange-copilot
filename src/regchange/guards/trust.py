"""신뢰 등급 태깅 — 외부에서 온 텍스트만 타입으로 구별한다 (R-23 ②).

목적:
    인젝션 스캔 대상이 되는 텍스트를 `str` 이 아니라 **별도 타입**으로 만든다.
    스캐너는 이 타입만 받고, 사내 문서 텍스트로는 이 타입을 만들 수 없다.

구현 이유:
    R-23 의 조치 ②는 "스캔 범위를 untrusted 로 제한한다"이고, 그 제한을 **무엇으로
    강제하는가**가 실질이다. 후보는 셋이었다.

      1. **호출부가 등급을 인자로 넘긴다** — 안 드러난다. trusted 를 untrusted 로 넘기면
         오탐이 늘고, 반대로 넘기면 조용히 스캔이 꺼진다. **양쪽 다 조용하다.**
      2. **label→등급 매핑표** — 안 드러난다. label 이 늘 때 표를 안 고치면 "기본값이
         무엇이냐"가 곧 안전 여부가 된다. **빠뜨리면 새는 쪽으로 기운다.**
      3. **타입으로 구별** — **틀린 호출이 타입 검사를 통과하지 못한다.** mypy strict 가
         이미 CI 게이트다.

    셋째를 택했다. **검증을 통과하는 것보다 표현 불가능한 것이 낫다** — 이 저장소가
    반복해 온 판단이다. `mst_resolution_source` 를 호출부가 주장하지 못하게 파생시킨 것,
    권한을 DB role 로 막은 것, 조문 `valid_from` 을 CHECK 로 막은 것이 같은 계열이다.

    **타입만으로는 부족하다 — 생성 지점을 좁힌다.** `UntrustedText(policy_text, ...)`
    를 아무 데서나 만들 수 있으면 타입은 장식이다. 그래서 세 겹을 둔다:

      1. 생성자가 모듈 private 토큰(`_MINT`)을 요구한다. 밖에서 직접 부르면 실패한다.
      2. 팩토리가 label 을 **등재된 외부 유입 경로**로 제한한다. `policy_candidates`
         라벨로는 만들어지지 않는다 (`TrustError`).
      3. 저장소 전체를 훑어 "등재되지 않은 곳에서 만들지 않는다"를 테스트가 고정한다
         (`tests/security/test_trust_boundary.py`). 파이썬에서 1·2 는 우회 가능하고,
         우회 가능한 것을 방어라고 부르지 않기 위해 3 을 둔다.

    **DB 의 `trust_level` 컬럼(012)과 이 모듈의 관계**: 등급을 선언하는 것은 스키마이고
    (CHECK 로 한 값에 고정), 이 모듈은 그 선언을 코드 쪽에서 거울처럼 들고 있다. 둘이
    어긋나면 `tests/security/test_trust_boundary.py` 가 실패한다. **런타임 코드는 아직
    그 컬럼을 읽지 않는다** — 등급이 문서 종류로 결정되어 행마다 다르지 않기 때문이다.
    행마다 달라지는 유입(첨부 문서)이 생기는 순간 읽어야 하며, 그것은 그때의 결정이다.

트레이드오프:
    - 값 객체 하나와 팩토리 하나가 늘고, 호출부가 `str` 대신 이 타입을 조립해야 한다.
      마찰을 대가로 **틀린 호출이 컴파일되지 않는 성질**을 얻었다.
    - 등급이 두 값(외부/내부)뿐이다. 세 번째 등급(예: 반신뢰 첨부문서)이 생기면 이
      타입 하나로는 부족하고 구조를 다시 봐야 한다. 지금 세 등급을 상정해 일반화하면
      **관측되지 않은 등급의 처리 규칙**을 지어내게 된다.
    - 신뢰 등급이 `text` 필드에 붙지 프로세스 전체에 전파되지 않는다. 이 타입에서
      `.text` 를 꺼내면 태그는 사라진다. 전파를 막지 않는 이유는 프롬프트 조립이
      문자열 연산이기 때문이며, 그래서 **꺼내는 지점을 wrap 하나로 좁혔다**.

엣지 케이스:
    - 빈 텍스트: `ValueError`. 빈 외부 입력은 상위 계층의 버그이며 조용히 통과시키면
      "스캔했는데 신호가 없었다"와 구별되지 않는다.
    - 등재되지 않은 label: `TrustError`. 새 외부 유입 경로는 `UNTRUSTED_LABELS` 에
      **명시적으로 등재해야** 한다. 등재를 잊으면 실패하지 통과하지 않는다.
    - 사내 문서 label 로 생성 시도: `TrustError`. 이것이 R-23 이 실제로 겪은 실수다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class TrustLevel(StrEnum):
    """문서가 어디서 왔는가. `regulation_document` / `policy_document` 의 컬럼과 같은 값이다."""

    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


class TrustError(RuntimeError):
    """신뢰 등급 경계를 어기는 생성 시도. 조용히 통과시키지 않는다."""


UNTRUSTED_LABELS: Final[frozenset[str]] = frozenset({"amended_article"})
"""외부 유입 경로의 label 목록. **이것이 스캔 대상의 전부다.**

지금은 하나뿐이다 — 법제처 API 가 준 개정 조문. 사내 규정(`policy_candidates`)은
여기 없으며, 없다는 것이 R-23 의 조치다. 첨부 문서 같은 새 경로가 생기면 여기 등재하고
그때 감도를 다시 잰다(범위가 바뀌면 오탐 분포가 통째로 달라진다)."""

TRUSTED_LABELS: Final[frozenset[str]] = frozenset({"policy_candidates"})
"""사내 문서 label. 스캔 대상이 아니며, 이 label 로는 `UntrustedText` 가 만들어지지 않는다.

목록으로 둔 이유는 **틀린 생성 시도를 이름으로 구별해 실패시키기 위해서**다.
단순히 "등재되지 않았다"로 실패시켜도 막히지만, 그러면 오류 메시지가 오타와
등급 위반을 구별하지 못한다."""

_MINT: Final = object()
"""생성 토큰. 이 모듈 밖에서는 이 값을 얻을 수 없으므로 팩토리를 거치지 않은 생성이 실패한다."""


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """외부에서 온 텍스트. **인젝션 스캔은 이 타입만 받는다** (R-23 ②).

    `str` 을 스캐너에 넘기면 mypy 가 막는다. 그것이 이 타입의 전부이자 목적이다.
    """

    text: str
    label: str
    _mint: object

    def __post_init__(self) -> None:
        """팩토리를 거치지 않은 생성을 거부한다."""
        if self._mint is not _MINT:
            msg = (
                "UntrustedText 를 직접 만들 수 없다. `guards.trust.from_regulation` 을 쓴다 — "
                "생성 지점이 넓어지면 사내 문서가 스캔 대상이 되는 경로가 다시 생긴다 (R-23)"
            )
            raise TrustError(msg)


def from_regulation(text: str, *, label: str) -> UntrustedText:
    """`regulation_*` 계열에서 온 텍스트를 스캔 대상으로 태깅한다.

    목적:
        외부 유입 텍스트에만 붙는 태그를 만든다. 이 함수가 `UntrustedText` 의
        **유일한 생성 경로**다.

    구현 이유:
        이름을 출처로 지었다(`from_regulation`). `from_text` 나 `tag` 였다면 사내
        문서에도 자연스럽게 쓰이고, 그 호출은 리뷰에서 이상해 보이지 않는다. 출처를
        이름에 박아 두면 **잘못된 호출이 읽는 순간 어색해진다.**

        label 을 검사하는 이유는 R-23 이 겪은 실수가 정확히 그 형태였기 때문이다 —
        사내 문단이 외부 텍스트와 같은 취급을 받았다. 지금은 `policy_candidates` 로
        부르면 `TrustError` 다.

    트레이드오프:
        label 목록을 손으로 유지해야 한다. 매핑표 안(3안)을 기각한 이유가 "표를 손으로
        유지해야 한다"였는데 여기에도 목록이 남는다. 차이는 **빠뜨렸을 때의 방향**이다 —
        매핑표는 빠뜨리면 조용히 스캔이 꺼지고, 이 목록은 빠뜨리면 `TrustError` 로
        멈춘다. 목록의 존재가 아니라 실패 방향이 문제였다.

    엣지 케이스:
        - 빈 텍스트/공백만: `ValueError`.
        - 사내 문서 label: `TrustError`. 오타와 구별되는 메시지를 준다.
        - 등재되지 않은 label: `TrustError`. 새 유입 경로는 등재가 먼저다.
    """
    if not text.strip():
        msg = f"{label}: 외부 텍스트가 비어 있다. 빈 입력을 스캔하면 '신호 없음'과 구별되지 않는다"
        raise ValueError(msg)
    if label in TRUSTED_LABELS:
        msg = (
            f"'{label}' 은 사내 문서(trusted)다. 스캔 대상으로 태깅할 수 없다 — "
            "R-23 이 관측한 실수가 정확히 이것이다"
        )
        raise TrustError(msg)
    if label not in UNTRUSTED_LABELS:
        msg = (
            f"'{label}' 은 등재된 외부 유입 경로가 아니다. "
            "guards.trust.UNTRUSTED_LABELS 에 등재하고, 범위가 바뀌었으므로 감도를 다시 잰다"
        )
        raise TrustError(msg)
    return UntrustedText(text, label, _MINT)
