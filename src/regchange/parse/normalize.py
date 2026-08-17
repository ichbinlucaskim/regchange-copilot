"""텍스트 정규화 `norm-v2`와 개정 마커 추출 (ADR-002).

목적:
    조문 텍스트에서 비교에 방해가 되는 요소(번호 접두어, 개정 마커, 공백 변형)를
    제거한 `text_norm`을 만들고, 제거한 마커를 구조화해 돌려준다.

구현 이유:
    **마커 판별 기준을 키워드가 아니라 "날짜 형태 포함 여부"로 둔다.** 실측에서
    `<개정 …>`·`<신설 …>` 외에 키워드 없는 날짜 마커 `<2013.8.13>`가 556건
    나왔고(`② 삭제 <2013.8.13>` 형태), 반대로 `<54>`(항번호 대체 표기, 100종
    578건)와 `<img …>`(43건)는 마커가 아니다. 날짜 유무가 이 둘을 가르는 선이다.

    ADR-002가 정한 `norm-v1`은 `개정/신설/삭제` 키워드만 상정했다. 실측상
    `<삭제 …>` 패턴은 **0건**이며 삭제는 "삭제" 단어 + 키워드 없는 날짜 마커로
    표현된다. 규칙을 바꿨으므로 버전을 `norm-v2`로 올린다 — `norm_rule_version`
    컬럼이 존재하는 이유가 여기서 증명된다.

트레이드오프:
    날짜 형태를 기준으로 삼으면, 날짜를 포함한 비-마커 꺾쇠 표현이 있을 경우
    잘못 제거한다. 관측 범위에서는 그런 사례가 없었고, 제거된 것은 `raw`로 전부
    보존되므로 사후에 복원·재검증할 수 있다. 반대로 키워드 기준을 유지하면
    556건의 날짜 마커가 텍스트에 남아 개정 때마다 가짜 MODIFIED가 난다(R-14).

엣지 케이스:
    - `<단서 생략>`(97건): 날짜가 없으므로 **제거하지 않고 추출만** 한다.
    - `<54>`·`<img …>`: 마커가 아니다. 그대로 둔다. `img id`는 두 번의 호출과
      하루 전 픽스처에서 모두 같은 값이었으므로 보존해도 가짜 변경을 만들지 않는다.
    - 복수 날짜(`<개정 2014.3.24, 2020.2.4, 2023.3.14>`)를 전제한다.
    - 날짜 파싱 실패: 조용히 넘기지 않고 `dates`를 비운 채 `raw`만 보존한다.
    - 한자·괄호·인용부호는 변형하지 않는다. `병과(竝科)`, `「법률명」`을 정규화하면
      서로 다른 조문이 같은 해시를 가질 수 있다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re

from regchange.parse.models import AmendmentMarker, MarkerType, NormalizedText

logger = logging.getLogger(__name__)

NORM_RULE_VERSION = "norm-v2"
"""정규화 규칙 버전. 규칙이 바뀌면 올리고 과거 행은 그대로 둔다 (ADR-002)."""

_DATE = r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?"
_DATE_RE = re.compile(_DATE)

_MARKER_RE = re.compile(r"<([^<>]{1,200}?)>")
"""꺾쇠 토큰 전체. 마커 여부는 내용을 보고 판정한다."""

_KEYWORD_MARKERS = {"개정": MarkerType.AMENDED, "신설": MarkerType.INSERTED}

_PROVISO_RE = re.compile(r"^단서\s*생략$")

_CIRCLED = "".join(chr(c) for c in range(0x2460, 0x246F))
"""①~⑮. 항번호는 원문자로 표기되며 15를 넘으면 `<54>` 형태가 된다."""

_PREFIX_RE = re.compile(
    rf"^\s*(?:[{_CIRCLED}]|\d{{1,3}}\.|[가-힣]\.)\s*",
)
"""항/호/목 내용 앞의 번호 접두어. 번호는 이미 별도 필드에 있다 (edge-case #6)."""


def _parse_dates(text: str) -> tuple[dt.date, ...]:
    out: list[dt.date] = []
    for raw in _DATE_RE.findall(text):
        parts = [p for p in re.split(r"\D+", raw) if p]
        if len(parts) != 3:
            continue
        try:
            out.append(dt.date(int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            logger.warning("마커 날짜 파싱 실패: %r", raw)
    return tuple(out)


def _classify(body: str) -> tuple[MarkerType, bool] | None:
    """꺾쇠 내용을 마커로 분류한다. 반환값 두 번째는 '텍스트에서 제거할지' 여부."""
    stripped = body.strip()
    if _PROVISO_RE.match(stripped):
        return MarkerType.PROVISO_OMITTED, False
    if not _DATE_RE.search(stripped):
        return None
    head = stripped.split()[0] if stripped.split() else ""
    for keyword, kind in _KEYWORD_MARKERS.items():
        if head.startswith(keyword):
            return kind, True
    return MarkerType.UNKNOWN_DATE, True


def extract_markers(text: str) -> tuple[str, tuple[AmendmentMarker, ...]]:
    """꺾쇠 마커를 추출하고, 제거 대상인 것만 제거한 텍스트를 함께 반환한다.

    목적:
        비교용 텍스트에서 개정 이력 표기를 걷어내되, 그 표기를 잃지 않는다.

    구현 이유:
        제거와 추출을 한 번의 스캔으로 처리한다. 두 번 스캔하면 정규식이 갈라져
        "추출은 됐는데 제거는 안 된" 상태가 생길 수 있고, 그러면 `amendment_markers`와
        `text_norm`이 어긋난다.

    트레이드오프:
        `<단서 생략>`처럼 추출은 하되 제거하지 않는 종류가 있어 반환 규약이
        단순하지 않다. 대신 마커 목록이 실제 본문에 무엇이 있었는지를 온전히 담는다.

    엣지 케이스:
        - 마커가 아닌 꺾쇠(`<54>`, `<img …>`)는 손대지 않는다.
        - 중첩·비정상 꺾쇠는 정규식이 매칭하지 않으므로 그대로 남는다.
    """
    markers: list[AmendmentMarker] = []
    removals: list[tuple[int, int]] = []
    for match in _MARKER_RE.finditer(text):
        classified = _classify(match.group(1))
        if classified is None:
            continue
        kind, remove = classified
        markers.append(
            AmendmentMarker(type=kind, dates=_parse_dates(match.group(1)), raw=match.group(0))
        )
        if remove:
            removals.append(match.span())

    if not removals:
        return text, tuple(markers)
    out: list[str] = []
    cursor = 0
    for start, end in removals:
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    return "".join(out), tuple(markers)


def normalize(raw: str, *, strip_prefix: bool = True) -> NormalizedText:
    """`norm-v2`를 적용해 원문·정규화본·해시·마커를 함께 담아 반환한다.

    목적:
        같은 조문의 두 버전을 비교했을 때 실질 변경이 있을 때만 다르게 보이도록
        텍스트를 정리한다.

    구현 이유:
        원문을 인자로 받아 원문을 그대로 되돌려준다(`raw`). 인용과 화면 표시는
        항상 원문을 가리켜야 하기 때문이다 — 담당자가 법령 원문을 열었을 때 우리가
        보여준 문장이 그대로 있어야 검증 비용이 오르지 않는다 (ADR-002).

    트레이드오프:
        같은 텍스트를 두 벌 들고 다녀 메모리와 저장 용량이 는다. 그 대신 인용·비교·
        재현이라는 세 요구가 서로 충돌하지 않게 분리된다.

    엣지 케이스:
        - 빈 문자열: 빈 정규화본과 그 해시를 그대로 반환한다. 예외를 던지지 않는다 —
          항번호 없는 껍데기 항의 `항내용`이 실제로 빈 문자열이다(51건).
        - `strip_prefix=False`: 조문내용처럼 번호 접두어가 제목의 일부인 경우에 쓴다.
    """
    stripped, markers = extract_markers(raw)
    if strip_prefix:
        stripped = _PREFIX_RE.sub("", stripped)
    norm = re.sub(r"\s+", " ", stripped).strip()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return NormalizedText(
        raw=raw,
        norm=norm,
        sha256=digest,
        rule_version=NORM_RULE_VERSION,
        markers=markers,
    )
