"""법령 본문 XML(`target=law` / `target=eflaw`)을 조문 트리로 파싱한다.

목적:
    `<법령>` 응답을 받아 `LawDocument`를 만든다. 조문·항·호·목 트리, 편장절관 계층,
    정규화 텍스트, 이동 참조를 모두 채운다.

구현 이유:
    표준 XML 파서를 쓰고 정규식으로 XML을 파싱하지 않는다. 응답에 CDATA 블록이
    파일당 1,126개 있어(edge-case #16), 정규식으로 자르면 본문에 `<![CDATA[`가
    섞이거나 본문 안의 `<개정 …>` 마커를 태그로 오인한다.

    파서는 `defusedxml`을 쓴다. 입력이 외부 HTTP 응답이므로 XXE·확장 폭탄
    (billion laughs)에 노출된다. 지금 출처가 법제처 한 곳이라도, 응답을 신뢰
    범위로 두는 것은 원칙 5(경계는 코드가 아니라 구조로)와 어긋난다.
    `scripts/`의 탐색 도구는 로컬 픽스처만 읽으므로 표준 파서를 유지한다.

트레이드오프:
    의존성이 하나 늘고, `defusedxml`이 일부 API를 막아 향후 필요한 기능(예:
    커스텀 엔티티)을 못 쓸 수 있다. 관측 범위에서 필요한 기능은 없다.

엣지 케이스:
    - `<목>`은 문서 순서로 직전 `<호>`에 귀속된다. 선행 호가 없으면 `ParseError`.
    - `조문내용`은 `<항>` 유무로 의미가 다르다. 항이 있으면 제목 줄만 들어 있다.
    - 조문키 길이가 7이 아니거나 재구성이 어긋나면 `ParseError`. 조용히 잘라내지
      않는다 (ADR-001).
    - 유형 자리가 `0`/`1`이 아니면 `ParseError`. 기본값으로 ARTICLE을 넣지 않는다.
    - 편장절관 제목행은 뒤따르는 조문과 조문키를 공유하므로 `seq_in_doc`으로 구분한다.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError as XmlParseError

from defusedxml.ElementTree import fromstring as _safe_fromstring

from regchange.parse.models import (
    ArticleUnit,
    Hang,
    Ho,
    LawDocument,
    Mok,
    UnitType,
)
from regchange.parse.normalize import normalize
from regchange.parse.references import parse_move_references

ARTICLE_KEY_LENGTH = 7
"""조문키 = 조문번호 4 + 가지번호 2 + 유형 1 (ADR-001, spec §4.2)."""

_TYPE_DIGITS = {"1": UnitType.ARTICLE, "0": UnitType.HEADING}

_HEADING_RE = re.compile(r"^\s*제\s*(\d+)\s*(편|장|절|관)")
"""편장절관 제목행. 계층은 XML 구조가 아니라 이 텍스트에만 있다 (edge-case #9)."""

_HEADING_DEPTH = {"편": 0, "장": 1, "절": 2, "관": 3}


class ParseError(ValueError):
    """응답이 상정한 구조를 벗어났을 때 발생한다. 조용히 넘기지 않는다."""


def _text(element: Element | None) -> str:
    return element.text or "" if element is not None else ""


def _find_text(parent: Element, tag: str) -> str:
    return _text(parent.find(tag))


def _parse_date(value: str) -> dt.date | None:
    digits = value.strip()
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def _parse_hang(hang_element: Element, article_key: str) -> Hang:
    """`<항>` 하나를 파싱한다. 목은 문서 순서로 직전 호에 귀속시킨다."""
    num_element = hang_element.find("항번호")
    hos: list[Ho] = []
    pending_moks: list[list[Mok]] = []

    for child in hang_element:
        if child.tag == "호":
            hos.append(
                Ho(
                    num=_find_text(child, "호번호").strip(),
                    text=normalize(_find_text(child, "호내용")),
                )
            )
            pending_moks.append([])
        elif child.tag == "목":
            if not hos:
                raise ParseError(
                    f"{article_key}: 선행 <호> 없이 <목>이 나왔다. "
                    "문서 순서 귀속 전제가 깨졌으므로 파서를 고쳐야 한다 (edge-case #4)"
                )
            pending_moks[-1].append(
                Mok(
                    num=_find_text(child, "목번호").strip(),
                    text=normalize(_find_text(child, "목내용")),
                )
            )

    merged = tuple(
        Ho(num=ho.num, text=ho.text, moks=tuple(moks))
        for ho, moks in zip(hos, pending_moks, strict=True)
    )
    return Hang(
        num=_text(num_element).strip() or None if num_element is not None else None,
        text=normalize(_find_text(hang_element, "항내용")),
        hos=merged,
    )


def _parse_unit(element: Element, seq: int, heading_stack: list[str]) -> ArticleUnit:
    """`<조문단위>` 하나를 파싱하고 편장절관 스택을 갱신한다."""
    article_key = element.get("조문키") or ""
    if len(article_key) != ARTICLE_KEY_LENGTH:
        raise ParseError(f"조문키 길이가 {ARTICLE_KEY_LENGTH}이 아니다: {article_key!r}")

    type_digit = article_key[-1]
    if type_digit not in _TYPE_DIGITS:
        raise ParseError(
            f"{article_key}: 유형 자리가 0/1이 아니다({type_digit!r}). "
            "알 수 없는 유형을 인용 대상 후보에 섞지 않는다 (ADR-001)"
        )
    unit_type = _TYPE_DIGITS[type_digit]

    no_text = _find_text(element, "조문번호").strip()
    branch_text = _find_text(element, "조문가지번호").strip() or "0"
    if not no_text.isdigit() or not branch_text.isdigit():
        raise ParseError(f"{article_key}: 조문번호/가지번호가 숫자가 아니다")
    article_no, branch_no = int(no_text), int(branch_text)

    rebuilt = f"{article_no:04d}{branch_no:02d}{type_digit}"
    if rebuilt != article_key:
        raise ParseError(
            f"조문키 재구성 불일치: 원본 {article_key} vs 재구성 {rebuilt}. "
            "자릿수 가정이 깨졌으므로 조용히 진행하지 않는다 (ADR-001)"
        )

    content_raw = _find_text(element, "조문내용")
    if unit_type is UnitType.HEADING:
        match = _HEADING_RE.match(content_raw)
        if match:
            depth = _HEADING_DEPTH[match.group(2)]
            del heading_stack[depth:]
            heading_stack.append(re.sub(r"\s+", " ", content_raw).strip())

    hangs = tuple(_parse_hang(h, article_key) for h in element.findall("항"))
    reference_raw = _find_text(element, "조문참고자료").strip() or None

    return ArticleUnit(
        article_key=article_key,
        article_no=article_no,
        branch_no=branch_no,
        unit_type=unit_type,
        seq_in_doc=seq,
        title=_find_text(element, "조문제목").strip() or None,
        content=normalize(content_raw, strip_prefix=False),
        hangs=hangs,
        heading_path=tuple(heading_stack),
        effective_date=_parse_date(_find_text(element, "조문시행일자")),
        changed=_find_text(element, "조문변경여부").strip() == "Y",
        reference_raw=reference_raw,
        moves=parse_move_references(reference_raw),
    )


def parse_law_document(source: str | Path) -> LawDocument:
    """법령 본문 XML을 `LawDocument`로 파싱한다.

    목적:
        수집한 응답(또는 픽스처)을 diff·적재가 쓸 수 있는 트리로 바꾼다.

    구현 이유:
        경로와 문자열을 모두 받는다. 개발과 테스트는 픽스처 파일로, 운영은 응답
        문자열로 들어오는데 두 경로가 같은 코드를 타야 픽스처 기반 테스트가
        운영 동작을 실제로 보증한다.

    트레이드오프:
        인자 하나가 두 의미를 갖는다. 파일이 존재하지 않는 짧은 문자열은 XML로
        간주되어 파싱 오류가 난다. 대신 호출부가 단순해진다.

    엣지 케이스:
        - 루트가 `<법령>`이 아니면 `ParseError`. 행정규칙(`AdmRulService`)이
          들어오면 여기서 막힌다 (ADR-006).
        - `<조문단위>`가 0건이면 `ParseError`. 조문 0건 수집은 성공이 아니라
          실패다 (ADR-005).
    """
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    else:
        candidate = Path(source)
        text = (
            candidate.read_text(encoding="utf-8")
            if len(source) < 4096 and candidate.exists()
            else source
        )

    try:
        root = _safe_fromstring(text)
    except XmlParseError as exc:
        raise ParseError(f"XML 파싱 실패: {exc}") from exc

    if root.tag != "법령":
        raise ParseError(
            f"루트 태그가 <법령>이 아니다: <{root.tag}>. "
            "행정규칙은 이 파서의 대상이 아니다 (ADR-006)"
        )

    basic = root.find("기본정보")
    if basic is None:
        raise ParseError("<기본정보>가 없다")

    units: list[ArticleUnit] = []
    heading_stack: list[str] = []
    for seq, element in enumerate(root.iter("조문단위")):
        units.append(_parse_unit(element, seq, heading_stack))

    if not units:
        raise ParseError("조문단위가 0건이다. 수집 실패로 취급한다 (ADR-005)")

    ministry_element = basic.find("소관부처")
    return LawDocument(
        law_id=_find_text(basic, "법령ID").strip(),
        law_name=_find_text(basic, "법령명_한글").strip(),
        law_kind=_find_text(basic, "법종구분").strip() or None,
        ministry=_text(ministry_element).strip() or None,
        # 속성이다. `find("소관부처코드")`로는 잡히지 않는다 (ADR-001의 조문키와 같은 함정).
        ministry_code=(
            None if ministry_element is None else ministry_element.get("소관부처코드") or None
        ),
        revision_kind=_find_text(basic, "제개정구분").strip() or None,
        promulgation_date=_parse_date(_find_text(basic, "공포일자")),
        document_effective_date=_parse_date(_find_text(basic, "시행일자")),
        units=tuple(units),
    )
