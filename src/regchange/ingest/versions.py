"""직전 버전의 MST를 확보한다 (R-21 해소 경로).

목적:
    새 MST 하나를 받아 그 **직전 버전의 MST**와 식별 정보를 돌려준다.
    `lsJoHstInf` 폴링이 알려 주는 것은 새 MST 하나뿐이므로, 결정론적 diff의
    입력 두 벌 중 한쪽을 이 모듈이 만든다.

구현 이유:
    `lawService.do?target=oldAndNew&MST=<새 MST>`의 `구조문_기본정보`가 직전 버전을
    담는다는 것을 2026-08-19에 실측했다 (`law-api-spec.md` §5.6.2, 표본 2건 —
    285199→283843, 283843→282481). `efYd` 소급 조회는 동작하지 않으므로(§3.2)
    이 경로가 확인된 유일한 방법이다.

    **한 단계만 따라간다.** 연쇄를 이 모듈이 반복하지 않는 이유는 두 가지다.
    (a) 운영 diff가 필요로 하는 것이 정확히 한 단계다. (b) 반복을 넣는 순간
    순환 방지와 깊이 상한이 필요해지는데, **필요 없는 방어 코드를 두지 않는 것이
    두는 것보다 낫다.** 여러 단계가 필요해지면 호출부가 반복하고, 멈춤 조건을
    그 호출부의 맥락에서 정한다 — "제정본까지"와 "특정 날짜까지"는 다른 조건이다.

    **파싱을 I/O에서 분리했다.** `parse_previous_version`은 순수 함수이며 픽스처로
    전수 검증된다. 네트워크가 필요한 부분은 `resolve_previous_mst` 하나뿐이다.

트레이드오프:
    `Collection`이 루트 `Element`를 노출하지 않으므로 `bodies[0]`을 다시 파싱한다.
    같은 문자열을 두 번 파싱하는 비용을 지불하는 대신, **스냅샷에 저장되는 바로 그
    문자열**을 파싱한다 — 감사에서 제시할 파일과 우리가 읽은 값의 출처가 같다.

    응답의 조문 대비표(`구조문목록`/`신조문목록`)를 쓰지 않는다. 그 목록은 조문키가
    없는 대비표 행이고 `(생 략)`으로 접혀 있어 원문이 아니다 (§5.6.3). 이 모듈은
    **식별자만** 가져오고 원문은 `law` 본문 조회가 담당한다.

엣지 케이스:
    - **제정본**: `구조문_기본정보`가 사라지지 않는다. **존재하되 sentinel로 채워져
      온다** — `법령일련번호=0`, 나머지 필드는 문자열 `"null"`. 그리고 그때만
      `신구법존재여부` 요소가 나타나고 값이 `N`이다 (실측:
      `oldandnew_288527_enacted.xml`). **`법령일련번호`를 그냥 읽으면 `"0"`이라는
      비어 있지도 않고 그럴듯한 값이 하류로 흘러간다** — 이 모듈이 막는 조용한 실패의
      본체다. `신구법존재여부 == "N"`이면 `None`을 돌려준다.
    - **`None`과 실패를 구별한다.** `None`은 "직전 버전이 없다"(제정본)이고 정상이다.
      "확인할 수 없다"(응답 형태 실패, 구조 불일치, MST 어긋남)는 예외다.
      두 경우를 같은 값으로 두면 제정본과 API 장애가 섞인다.
    - **요청 MST와 응답의 신조문 MST가 다르면 예외.** API가 다른 버전을 준 것이며
      조용히 넘기면 그 시점부터 모든 diff가 틀린 짝으로 계산된다.
    - `신구법존재여부`가 `N` 이외의 값이면 예외. 관측된 적 없는 형태이며, 모르는
      값을 정상으로 해석하지 않는다.
    - sentinel(`법령일련번호=0`)인데 `신구법존재여부`가 없으면 예외. 두 신호가
      어긋나는 형태는 관측된 적이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from xml.etree.ElementTree import Element, ParseError, fromstring

import structlog

from regchange.adapters.storage.base import DocumentStore
from regchange.ingest.client import Collection, CollectionFailure, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import OLD_AND_NEW_DOCUMENT
from regchange.ingest.snapshot import write_snapshot

_log = structlog.get_logger(__name__)

NO_PREVIOUS_MARKER = "신구법존재여부"
"""직전 버전이 없을 때만 나타나는 요소. 값은 `N`이다.

**신구법이 있는 응답에는 이 요소가 아예 없다** (실측 5표본: 법령 2 · 행정규칙 3).
활용가이드는 이 필드를 항상 있는 것처럼 설명하지만 관측은 그렇지 않다."""

NO_PREVIOUS_VALUE = "N"
"""`신구법존재여부`의 관측된 유일한 값. 다른 값은 미지의 형태로 다룬다."""

SENTINEL_MST = "0"
"""직전 버전이 없을 때 `구조문_기본정보/법령일련번호`에 오는 값.

**빈 문자열이 아니라 `"0"`이다.** 비어 있는지만 검사하면 통과해 버리고, 그 값이
MST로 하류에 흘러간다. `oldandnew_288527_enacted.xml` 참조."""

SENTINEL_NULL = "null"
"""sentinel 응답에서 문자열로 오는 값. XML의 빈 요소가 아니라 네 글자 `null`이다."""

_OLD = "구조문_기본정보"
_NEW = "신조문_기본정보"


class VersionResolutionError(RuntimeError):
    """직전 버전을 확인할 수 없다 — **"직전 버전이 없다"와 다른 값이다**."""


@dataclass(frozen=True, slots=True)
class VersionHeader:
    """한 버전의 식별 정보. `구조문_기본정보` / `신조문_기본정보` 한 블록에 대응한다.

    목적:
        MST와 함께 "그 MST가 무엇인가"를 담아, 잘못된 짝을 하류에서 검출할 수 있게 한다.

    구현 이유:
        MST 문자열만 돌려주면 호출부가 법령ID·공포일자를 다시 조회해야 하고, 그러면
        검증(§3-1 법령ID 일치, §3-2 공포일자 순서)이 별도 왕복이 된다. 같은 응답에
        이미 들어 있는 값을 함께 돌려주면 검증이 무료다.

    트레이드오프:
        날짜를 `date`가 아니라 8자리 문자열로 둔다. 법제처가 준 표기를 그대로 보존하는
        규칙(`snapshot.py`의 같은 판단)을 따른다 — 시행일 `20260820`은 "KST 자정"이
        아니라 "그날부터 효력"이라는 법적 사실이고 타임존 변환 대상이 아니다.
        `date`가 필요한 곳에서 변환한다.

    엣지 케이스:
        - 필드가 비어 있으면 `parse_previous_version`이 예외를 던진다. 여기서 빈
          문자열을 허용하면 sentinel 응답이 정상 헤더로 통과한다.
    """

    mst: str
    law_id: str
    promulgation_date: str
    promulgation_no: str
    effective_date: str
    revision_kind: str
    law_name: str


@dataclass(frozen=True, slots=True)
class PreviousVersion:
    """직전 버전과, 그것을 물어본 대상 버전.

    목적:
        "무엇의 직전인가"를 값 안에 남긴다.

    구현 이유:
        `requested`를 함께 담는 이유는 **응답이 우리가 물어본 것에 대한 답인지**를
        호출부가 확인할 수 있게 하기 위해서다. 파서가 이미 대조하지만, 값에 남겨 두면
        `change_set`에 기록할 때 다시 조회할 필요가 없다.

    트레이드오프:
        호출부가 대개 `previous.mst`만 쓰므로 나머지는 잉여로 보인다. 그 잉여가 곧
        검증 재료다 — 잉여를 없애면 검증이 왕복 조회가 된다.

    엣지 케이스:
        - 이 값이 만들어졌다는 것은 이미 MST 대조를 통과했다는 뜻이다.
          `requested.mst != <요청한 MST>`인 인스턴스는 존재할 수 없다.
    """

    previous: VersionHeader
    requested: VersionHeader


def _text(block: Element, tag: str) -> str:
    """블록에서 태그 하나를 읽어 공백을 정리한다. 없으면 빈 문자열."""
    return (block.findtext(tag) or "").strip()


def _header(block: Element, *, side: str) -> VersionHeader:
    """기본정보 블록을 `VersionHeader`로 만든다. 빈 값이 있으면 예외.

    sentinel 응답이 여기까지 오면 안 된다 — `parse_previous_version`이 먼저 걸러낸다.
    그래도 검사하는 이유는, 걸러내는 조건이 나중에 바뀌었을 때 sentinel이 조용히
    헤더로 승격되는 것을 막기 위해서다.
    """
    fields = {
        "mst": _text(block, "법령일련번호"),
        "law_id": _text(block, "법령ID"),
        "promulgation_date": _text(block, "공포일자"),
        "promulgation_no": _text(block, "공포번호"),
        "effective_date": _text(block, "시행일자"),
        "revision_kind": _text(block, "제개정구분명"),
        "law_name": _text(block, "법령명"),
    }
    empty = sorted(name for name, value in fields.items() if not value)
    if empty:
        raise VersionResolutionError(f"{side} 기본정보에 빈 필드가 있다: {empty}")
    if fields["mst"] == SENTINEL_MST or fields["law_id"] == SENTINEL_NULL:
        raise VersionResolutionError(
            f"{side} 기본정보가 sentinel 값이다 (mst={fields['mst']!r}, "
            f"law_id={fields['law_id']!r}). 직전 버전 없음 판정이 먼저 걸렀어야 한다"
        )
    return VersionHeader(**fields)


def parse_previous_version(body: str, *, requested_mst: str) -> PreviousVersion | None:
    """`oldAndNew` 본문 응답에서 직전 버전을 읽는다. 없으면 `None`.

    목적:
        응답 문자열 하나를 받아 직전 버전 식별 정보를 돌려주는 순수 함수.

    구현 이유:
        네트워크와 분리해 픽스처로 전수 검증할 수 있게 했다. 이 함수가 잘못되면
        **모든 diff가 틀린 짝으로 계산되고 결과는 그럴듯해 보인다.** 검증 비용을
        낮추는 것이 이 분리의 목적이다.

    트레이드오프:
        문자열을 받아 안에서 파싱한다. `Element`를 받으면 호출부가 파싱 방식을 고를
        수 있지만, 그러면 인코딩 처리가 두 곳으로 갈린다 (edge-case #15).

    엣지 케이스:
        - `신구법존재여부 == "N"`: **`None`.** 제정본이며 정상이다.
        - `신구법존재여부`가 있는데 값이 `N`이 아님: 예외. 미지의 형태다.
        - `구조문_기본정보/법령일련번호 == "0"`인데 `신구법존재여부`가 없음: 예외.
          두 신호가 어긋나는 형태는 관측된 적이 없고, 어느 쪽을 믿을지 근거가 없다.
        - 요청 MST != `신조문_기본정보/법령일련번호`: 예외. API가 다른 버전을 준 것이다.
        - XML 파싱 실패·루트 태그 불일치·블록 누락: 전부 예외.
    """
    try:
        root = fromstring(body)  # noqa: S314 — 법제처 응답. 분류기가 이미 형태를 검사했다
    except ParseError as exc:
        raise VersionResolutionError(f"XML 파싱 실패: {exc}") from exc

    if root.tag != OLD_AND_NEW_DOCUMENT.root_tag:
        raise VersionResolutionError(
            f"루트 태그가 <{root.tag}>다. <{OLD_AND_NEW_DOCUMENT.root_tag}>를 기대한다"
        )

    new_block = root.find(_NEW)
    if new_block is None:
        raise VersionResolutionError(f"<{_NEW}> 블록이 없다")
    requested = _header(new_block, side=_NEW)

    if requested.mst != requested_mst:
        raise VersionResolutionError(
            f"응답의 신조문 MST가 요청과 다르다: 요청 {requested_mst!r} / 응답 "
            f"{requested.mst!r}. 다른 버전의 직전을 돌려주면 이후 diff 전체가 틀린 짝이 된다"
        )

    old_block = root.find(_OLD)
    if old_block is None:
        raise VersionResolutionError(f"<{_OLD}> 블록이 없다")

    marker = root.findtext(NO_PREVIOUS_MARKER)
    sentinel = _text(old_block, "법령일련번호") == SENTINEL_MST

    if marker is not None:
        if marker.strip() != NO_PREVIOUS_VALUE:
            raise VersionResolutionError(
                f"<{NO_PREVIOUS_MARKER}> 값이 {marker.strip()!r}다. 관측된 값은 "
                f"{NO_PREVIOUS_VALUE!r} 뿐이며 미지의 값을 정상으로 해석하지 않는다"
            )
        if not sentinel:
            raise VersionResolutionError(
                f"<{NO_PREVIOUS_MARKER}>=N 인데 구조문 법령일련번호가 sentinel이 아니다. "
                "두 신호가 어긋나며 어느 쪽을 믿을 근거가 없다"
            )
        return None

    if sentinel:
        raise VersionResolutionError(
            f"구조문 법령일련번호가 {SENTINEL_MST!r}인데 <{NO_PREVIOUS_MARKER}>가 없다. "
            "직전 버전 없음의 두 신호가 어긋난다 — 이 값을 MST로 쓰면 조용히 실패한다"
        )

    return PreviousVersion(previous=_header(old_block, side=_OLD), requested=requested)


async def resolve_previous_mst(
    client: LawApiClient,
    store: DocumentStore,
    mst: str,
    *,
    run_id: str,
    fetched_at: datetime,
    masker: Masker,
) -> PreviousVersion | None:
    """직전 버전을 조회하고 스냅샷으로 남긴다. 제정본이면 `None`.

    목적:
        R-21의 해소 경로를 하나의 호출로 감싼다.

    구현 이유:
        **스냅샷을 반드시 쓴다.** 이 응답이 "왜 이 두 버전을 비교했는가"의 근거이며,
        남기지 않으면 6개월 뒤 감사에서 짝 선택의 출처를 제시할 수 없다.
        비교 결과(`change_set`)만 남기고 짝 선택 근거를 버리면 F-3(소명 불가)이다.

    트레이드오프:
        같은 MST를 다시 물으면 `write_snapshot`이 같은 디렉터리를 거부해 실패한다.
        중복 조회를 조용히 허용하는 대신 실패로 드러나게 했다 — 호출부가 결과를
        들고 다니면 될 일이며, 덮어쓰기 경로를 만들지 않는다는 규칙이 우선한다.

    엣지 케이스:
        - `CollectionFailure`: `VersionResolutionError`. **`None`이 아니다.**
        - 본문 계열이라 페이지가 항상 1개다. 0개면 성공한 수집이 아니다.
        - 제정본이어도 스냅샷은 쓴다. "물어봤고 없다는 답을 받았다"는 사실 자체가
          기록 대상이다 — 안 물어본 것과 구별되어야 한다.
    """
    outcome = await client.collect(OLD_AND_NEW_DOCUMENT, {"MST": mst})
    if isinstance(outcome, CollectionFailure):
        raise VersionResolutionError(
            f"직전 버전 조회 실패 (MST={mst}): {outcome.reason.value} / {outcome.detail}"
        )

    collection: Collection = outcome
    if not collection.bodies:
        raise VersionResolutionError(f"직전 버전 조회 응답에 페이지가 없다 (MST={mst})")

    await write_snapshot(
        store,
        collection,
        run_id=run_id,
        fetched_at=fetched_at,
        params={"MST": mst},
        display=None,
        masker=masker,
    )

    previous = parse_previous_version(collection.bodies[0], requested_mst=mst)
    _log.info(
        "versions.previous_resolved",
        mst=mst,
        previous_mst=None if previous is None else previous.previous.mst,
        law_id=None if previous is None else previous.previous.law_id,
        has_previous=previous is not None,
    )
    return previous
