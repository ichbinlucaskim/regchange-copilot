"""`조문참고자료`의 조문 이동 표기를 구조화한다.

목적:
    `[제6조에서 이동, 종전 제9조는 제12조로 이동 <2020.3.24>]` 같은 표기를
    `MoveReference`로 바꾼다.

구현 이유:
    법제처는 `조문이동이전`/`조문이동이후` 필드로는 이동을 주지 않지만
    (13개 문서 1,957 조문단위에서 비영값 0건), **`조문참고자료` 텍스트에는 명시한다**
    (픽스처 7개 법령 93건). 이 표기가 있으면 유사도 추론보다 훨씬 강한 근거다.

    다만 이것은 API가 구조화해 준 필드가 아니라 **우리가 텍스트에서 추출한 것**이다.
    ADR-007의 `PARSED` 식별자와 같은 지위이며, 파서가 틀리면 잘못된 이동이 된다.
    그래서 `raw`를 반드시 보존하고, 자동 확정하지 않는다 (ADR-003).

트레이드오프:
    정규식이 표기 변형에 관대해야 한다 — 조사가 흔들린다(`종전 제N조는/은`,
    `제N조로/으로`, `종전의`). 관대하게 만들수록 오탐 위험이 오르므로,
    **`제N조` 형태와 `이동`/`삭제` 키워드가 함께 있는 경우로 좁혔다.**
    그 결과 `[…개인형 이동장치는 제외한다…]`(도로교통법) 같은 오탐을 배제한다.

엣지 케이스:
    - 한 대괄호 안에 두 표기가 쉼표로 이어진다: `제6조에서 이동, 종전 제9조는 …`.
      각각을 별도 `MoveReference`로 낸다.
    - `종전 제34조의2는 삭제` — 이동이 아니라 삭제 표기다. 별도 종류로 구분한다.
    - **이동 표기는 도착 버전에만 있다.** 2011판 특금법에는 0건이고 2020판에 18건이다.
      출발 버전만 보고는 "이 조문이 어디로 갔는가"를 알 수 없다 (ADR-003 근거 b).
    - 매칭되지 않은 대괄호는 버리지 않고 호출자가 `reference_raw`로 보존한다.
"""

from __future__ import annotations

import re

from regchange.parse.models import ArticleRef, MoveKind, MoveReference
from regchange.parse.normalize import _DATE_RE, _parse_dates

_BRACKET_RE = re.compile(r"\[([^\[\]]{1,300})\]")

_ARTICLE = r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?"

_MOVED_FROM_RE = re.compile(rf"{_ARTICLE}\s*에서\s*이동")
"""`제6조에서 이동` — 이 조문이 종전 어느 번호에서 왔는가."""

_PREVIOUS_MOVED_TO_RE = re.compile(
    rf"종전의?\s*{_ARTICLE}\s*[은는]\s*{_ARTICLE}\s*(?:으)?로\s*이동"
)
"""`종전 제9조는 제12조로 이동` — 이 번호를 쓰던 조문이 어디로 갔는가."""

_PREVIOUS_DELETED_RE = re.compile(rf"종전의?\s*{_ARTICLE}\s*[은는]\s*삭제")
"""`종전 제34조의2는 삭제`."""


def _ref(no: str, branch: str | None) -> ArticleRef:
    return ArticleRef(article_no=int(no), branch_no=int(branch) if branch else 0)


def parse_move_references(reference_text: str | None) -> tuple[MoveReference, ...]:
    """`조문참고자료` 원문에서 이동·삭제 표기를 전부 추출한다.

    목적:
        diff 단계가 유사도 추론 없이도 이동 후보를 만들 수 있게 한다.

    구현 이유:
        대괄호 단위로 먼저 자른 뒤 그 안에서 패턴을 찾는다. 대괄호를 무시하고
        전체 텍스트에 정규식을 걸면 `[전문개정 2011.5.19]` 같은 다른 표기와 경계가
        섞이고, 본문에 우연히 들어간 문장을 잡을 위험이 커진다.

    트레이드오프:
        대괄호가 없는 형태의 이동 표기가 있다면 놓친다. 관측 범위(픽스처 7개 법령
        93건)에서는 전부 대괄호 안에 있었다. 놓친 경우는 `reference_raw`가 보존되어
        사후에 발견할 수 있다.

    엣지 케이스:
        - None 또는 빈 문자열: 빈 튜플을 반환한다.
        - 이동 키워드가 없는 대괄호(`[전문개정 2011.5.19]`): 무시한다.
        - `제N조` 없이 '이동'만 있는 문장: 매칭하지 않는다. 도로교통법의
          `[…개인형 이동장치는 제외한다…]`가 이 경우다.
    """
    if not reference_text:
        return ()

    found: list[MoveReference] = []
    for bracket in _BRACKET_RE.finditer(reference_text):
        body = bracket.group(1)
        if "이동" not in body and "삭제" not in body:
            continue
        dates = _parse_dates(body) if _DATE_RE.search(body) else ()
        raw = bracket.group(0)

        for match in _PREVIOUS_MOVED_TO_RE.finditer(body):
            found.append(
                MoveReference(
                    kind=MoveKind.PREVIOUS_MOVED_TO,
                    source=_ref(match.group(1), match.group(2)),
                    target=_ref(match.group(3), match.group(4)),
                    dates=dates,
                    raw=raw,
                )
            )
        for match in _PREVIOUS_DELETED_RE.finditer(body):
            found.append(
                MoveReference(
                    kind=MoveKind.PREVIOUS_DELETED,
                    source=_ref(match.group(1), match.group(2)),
                    dates=dates,
                    raw=raw,
                )
            )
        # `종전 …는 제N조로 이동`의 뒷부분이 `제N조에서 이동`과 겹치지 않도록,
        # 앞선 두 패턴이 소비한 구간을 지운 뒤 MOVED_FROM을 찾는다.
        residue = _PREVIOUS_MOVED_TO_RE.sub(" ", body)
        residue = _PREVIOUS_DELETED_RE.sub(" ", residue)
        for match in _MOVED_FROM_RE.finditer(residue):
            found.append(
                MoveReference(
                    kind=MoveKind.MOVED_FROM,
                    source=_ref(match.group(1), match.group(2)),
                    dates=dates,
                    raw=raw,
                )
            )
    return tuple(found)
