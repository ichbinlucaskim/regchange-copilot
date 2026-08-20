"""조문 전문 조립 — 조문내용 + 항 + 호 + 목을 하나의 비교 대상으로 잇는다.

목적:
    조문 하나의 텍스트 전체를 한 값으로 만든다. 변경 감지(diff)와 검색(retrieval)이
    **같은 텍스트**를 보게 하는 것이 이 모듈의 존재 이유다.

구현 이유:
    조립 규칙을 `parse`에 둔다. "조문내용과 항/호/목을 어떻게 이어붙이는가"는 법제처
    XML 구조에 대한 지식이고, 그 지식은 파싱 계층이 갖는다. 적재는 파싱 결과를
    저장하는 계층이지 해석하는 계층이 아니다.

    **규칙이 두 곳에 있으면 안 되는 이유가 이미 실증됐다.** 부처 코드↔이름 짝짓기가
    두 경로로 갈릴 뻔했고(ADR-009 엣지 케이스), 그 함정은 길이 검사에 걸리지 않아
    조용히 34%를 오배정했을 것이다. 조립도 같은 성질이다 — 검색된 텍스트와 diff한
    텍스트가 미묘하게 다르면 **인용이 가리키는 것과 변경 판정의 대상이 달라지고**,
    원칙 2(근거 강제)의 전제가 무너진다. 그 어긋남은 예외를 내지 않는다.

    `조문내용`의 의미가 `<항>` 유무로 달라지지만(edge-case #5), 조립 규칙은 분기하지
    않는다. 항이 없으면 `조문내용`이 전문이고, 항이 있으면 제목 줄이다. 어느 쪽이든
    **`조문내용` 다음에 모든 항/호/목을 문서 순서로 잇는 것**이 전문이 된다.
    분기하지 않는 편이 안전하다 — 분기하면 두 경로 중 하나만 고치는 사고가 생긴다.

트레이드오프:
    조문당 텍스트를 한 벌 더 만든다. `text_raw`(조문내용)와 내용이 겹치는 부분이
    있으므로 저장 용량이 는다. 그 대신 변경 감지가 제목만 비교하는 사고를 막는다 —
    실측으로 그 사고를 겪었다. 특금법 2011↔2020에서 `조문내용`만 비교하면
    일치율 0.7407(FN 5건), 전문을 조립하면 0.9259(FN 0건)다.

    항/호/목의 경계를 공백 하나로 뭉갠다. 따라서 조립된 텍스트만으로는 "제1항의
    어디까지가 제1호인가"를 복원할 수 없다. 인용 좌표는 조립본이 아니라 트리
    구조(`body` jsonb)가 갖는다 — 조립본은 **비교와 검색용**이다.

엣지 케이스:
    - 항이 없는 조문(4개 법령 245건): `조문내용`이 곧 전문이므로 그것만 담긴다.
    - 빈 껍데기 항(`항번호`가 None, 4개 법령 51건): `항내용`이 빈 문자열이라
      조립에 아무것도 더하지 않는다. 그 항에 달린 호/목은 그대로 이어진다.
    - 본문이 전부 빈 조문: 빈 문자열의 해시를 그대로 반환한다. 예외를 던지지
      않는다 — `normalize()`의 규약과 같다. 이런 조문은 유사도 후보에서 제외하는
      것이 호출자(ADR-003)의 책임이다.
    - 편장절관 제목행(`unit_type='HEADING'`): 항이 없으므로 제목 텍스트만 담긴다.
      인용 대상이 아니지만 조립 자체는 성립한다.
    - 마커는 전 계층에서 모아 순서대로 담는다. 조문내용에만 있는 마커를 보면
      항 안의 `<개정 …>`를 놓치고, 그러면 EDITORIAL 판정이 조용히 틀린다.
"""

from __future__ import annotations

import hashlib

from regchange.parse.models import AmendmentMarker, ArticleUnit, NormalizedText
from regchange.parse.normalize import NORM_RULE_VERSION

RAW_JOINER = "\n"
"""원문 조립 구분자. 항·호·목이 원래 줄 단위로 표시되므로 줄바꿈이 자연스럽다."""

NORM_JOINER = " "
"""정규화본 조립 구분자. `normalize()`가 이미 공백을 하나로 접었으므로 공백으로 잇는다."""


def _parts(unit: ArticleUnit) -> tuple[list[str], list[str], list[AmendmentMarker]]:
    """조문의 모든 텍스트 조각을 문서 순서로 모은다.

    목의 호 귀속은 파서가 이미 문서 순서로 확정했으므로(edge-case #4), 여기서는
    그 순서를 **바꾸지 않고 그대로 따라가기만 한다.** 정렬하거나 재배치하면 파서가
    지킨 귀속이 이 함수에서 무너진다.
    """
    raws: list[str] = []
    norms: list[str] = []
    markers: list[AmendmentMarker] = []

    def add(text: NormalizedText) -> None:
        if text.raw:
            raws.append(text.raw)
        if text.norm:
            norms.append(text.norm)
        markers.extend(text.markers)

    add(unit.content)
    for hang in unit.hangs:
        add(hang.text)
        for ho in hang.hos:
            add(ho.text)
            for mok in ho.moks:
                add(mok.text)
    return raws, norms, markers


def assemble_body(unit: ArticleUnit) -> NormalizedText:
    """조문 하나의 전문을 조립해 `NormalizedText`로 돌려준다.

    목적:
        변경 감지와 검색이 함께 쓸 조문 전체 텍스트와 그 해시를 만든다.

    구현 이유:
        `NormalizedText`를 그대로 재사용한다. 새 타입을 만들면 `text_raw`/`text_norm`을
        다루는 코드와 조립본을 다루는 코드가 갈라지고, 둘 중 하나에만 적용되는 규칙이
        생긴다. 같은 타입이면 `rule_version`으로 정규화 규칙 버전을 함께 들고 다닌다.

        해시는 **정규화본**으로 계산한다(`normalize()`와 같은 기준). 원문 해시를 쓰면
        공백·줄바꿈 차이가 변경으로 잡히고, 개정 마커의 날짜 변경이 실질 변경과
        구별되지 않는다 — R-14(가짜 알림 폭주)가 그대로 발생한다.

    트레이드오프:
        마커를 전 계층에서 모으므로 같은 마커가 여러 번 나타날 수 있다(항마다
        `<개정 2020.3.24>`가 붙는 경우). 중복을 제거하지 않는다 — **어느 계층에
        몇 개 붙었는지가 EDITORIAL 판정의 신호**이고, 중복을 지우면 "항 하나에만
        마커가 추가된 경우"와 "전 항에 추가된 경우"가 같아진다.

    엣지 케이스:
        - 텍스트가 전부 비어 있음: 빈 문자열의 sha256을 반환한다. 예외를 던지지 않는다.
        - 항이 없는 조문: `조문내용`만 담긴다 (edge-case #5).
        - 마커가 없음: 빈 튜플. `EDITORIAL` 판정에서 "마커 없음 vs 마커 없음"은
          변경 없음이다.
    """
    raws, norms, markers = _parts(unit)
    norm = NORM_JOINER.join(norms)
    return NormalizedText(
        raw=RAW_JOINER.join(raws),
        norm=norm,
        sha256=hashlib.sha256(norm.encode("utf-8")).hexdigest(),
        rule_version=NORM_RULE_VERSION,
        markers=tuple(markers),
    )
