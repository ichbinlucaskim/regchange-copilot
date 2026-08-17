"""페이지 병합 결과의 건수와 식별키를 단언한다. 조용한 누락을 막는 마지막 관문.

목적:
    수집 결과가 `totalCnt`와 일치하는지, 식별키가 충돌하지 않는지를 검사하고,
    **어긋나면 실패로 처리한다.** 경고가 아니다.

구현 이유:
    이 저장소의 조용한 누락 3건 중 하나(사건 2)가 정확히 이 지점에서 발생했다 —
    `display=100`으로 잘린 응답을 전수로 착각해 어휘를 4종으로 결론냈다.
    응답에 "잘렸다"는 표시가 없으므로(edge-case #11), **`totalCnt`와 대조하는
    것만이 잘림을 아는 방법이다.**

    검사를 `client`에서 분리한 이유는 **순수 함수로 만들어 네트워크 없이 전수
    검증하기 위해서다.** 잘린 픽스처 3종과 365일 캐시가 이미 저장소에 있으므로,
    이 모듈의 단언은 실제 데이터로 검증된다.

    **완주 검사 대상이 계열마다 다르다.** 이력 계열의 `totalCnt`는 조문 수가
    아니라 **법령 수**다 — `dayjochg_regdt20250401.xml`은 `totalCnt=83`인데 조문은
    286건이다. 조문 기준으로 검사하면 항상 실패하고, 그것을 맞추려고 임계값을
    느슨하게 하면 진짜 잘림을 놓친다 (재계산 판별 원칙 3: 재계산 대상이 무엇인지
    명시한다).

트레이드오프:
    `totalCnt`를 신뢰한다. 법제처가 그 값을 틀리게 주면 정상 수집이 실패로
    보고된다. **그 방향의 오류를 감수한 이유는 비대칭 때문이다** — 잘림을 놓치면
    "개정 없음"으로 위장하고 담당자는 확인할 것이 없다고 잘못 알지만, 잘못된
    실패는 시끄럽게 드러나 사람이 판단한다.

    **중복 제거를 하지 않는다.** 제거를 지원하지 않으므로 "진짜 중복이 있는데도
    적재된다"는 상태가 가능하다. 그러나 관측된 "중복"은 전부 서로 다른 시점
    정보였고(edge-case #18), 제거하면 `valid_from`이 소실된다. 제거 기능을 두면
    누군가 그것을 켠다.

엣지 케이스:
    - 본문 계열(`DOCUMENT`)은 `totalCnt`가 없으므로 **완주 검사 대상이 아니다.**
      건수 검사를 시도하면 항상 실패한다.
    - `totalCnt=0`이고 항목 0건: 완주로 본다. **0건이 정상인지 실패인지는 이
      모듈이 판단하지 않는다** — 이력 계열에서 그것은 응답으로 구별 불가능하며
      (`HISTORY_ZERO_IS_AMBIGUOUS`) 카나리아와 0건 재요청이 담당한다.
    - 수신 건수가 `totalCnt`보다 **많은** 경우도 실패다. 페이지 경계에서 중복
      수신했다는 뜻이며, 조용히 통과시키면 건수가 부풀려진다. 누락만 검사하면
      과잉을 놓친다.
    - 식별키를 구성할 필드가 비어 있으면 실패다. 빈 키끼리는 서로 같아 보여
      **한 건만 남은 것처럼 집계된다** — 사건 1(dict 붕괴)과 같은 기전이다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from xml.etree.ElementTree import Element

from regchange.ingest.response import ResponseFamily, TargetSpec


class IntegrityFailure(StrEnum):
    """무결성 검사가 실패한 사유. 각각 다른 운영 조치로 이어진다.

    목적:
        "왜 실패했는가"를 값으로 구별해, 로그를 보고 조치를 정할 수 있게 한다.

    구현 이유:
        하나의 불리언으로 두면 잘림과 중복 수신과 키 충돌이 같은 값이 된다.
        세 사유의 조치가 전혀 다르다 — 잘림은 재수집, 중복 수신은 페이지네이션
        버그, 키 충돌은 **식별키 재검토**다.

    트레이드오프:
        사유가 늘면 호출부 분기가 늘어난다. 그러나 어느 사유든 결과는 "실패"
        하나이므로, 호출부는 실패 여부만 보면 되고 사유는 로그와 조치용이다.

    엣지 케이스:
        - `MISSING_IDENTITY`는 데이터 문제이자 우리 spec 문제일 수 있다. 선언한
          경로가 틀렸을 때도 같은 증상이 나오므로, 로그에 경로를 함께 남긴다.
    """

    SHORT_COUNT = "SHORT_COUNT"
    """수신 건수가 `totalCnt`보다 적다. 잘렸다 — 페이지를 더 받아야 한다."""

    OVER_COUNT = "OVER_COUNT"
    """수신 건수가 `totalCnt`보다 많다. 페이지 경계에서 중복 수신했다."""

    KEY_COLLISION = "KEY_COLLISION"
    """식별키가 충돌했다. **식별키를 다시 검토할 신호다** (edge-case #18)."""

    MISSING_IDENTITY = "MISSING_IDENTITY"
    """식별키 필드가 비었다. spec의 경로가 틀렸거나 응답 구조가 바뀌었다."""


@dataclass(frozen=True, slots=True)
class RecordKey:
    """이력 레코드 하나의 식별키. 계열마다 구성 필드가 다르다.

    목적:
        "무엇을 같은 것으로 보는가"를 값으로 고정한다.

    구현 이유:
        **무엇을 키로 삼는가가 곧 무엇을 같은 것으로 보는가다.** 튜플로 두지 않고
        이름 있는 타입으로 만든 이유는, 충돌 로그에 어느 필드가 무엇이었는지를
        남겨야 하기 때문이다. 익명 튜플은 로그에서 의미를 잃는다.

    트레이드오프:
        필드 셋이 고정되어 있어 계열별로 다른 개수의 필드를 담지 못한다. 현재 두
        이력 계열이 모두 3필드(MST·조문번호·시점)이므로 맞고, 어긋나는 계열이
        생기면 그때 구조를 바꾼다 — 미리 일반화하면 어느 필드가 무슨 의미인지가
        흐려진다.

    엣지 케이스:
        - `version_time`의 의미가 계열마다 다르다(일자별 `조문시행일`, 조문별
          `조문변경일`). 필드명을 중립적으로 둔 이유이며, 어느 필드에서 왔는지는
          `source_field`에 남긴다.
    """

    version_id: str
    """법령일련번호(MST). 법령ID가 아니다 — 법령ID로 묶으면 연혁이 사라진다."""

    article_no: str
    """조문번호 6자리."""

    version_time: str
    """시점. 일자별은 `조문시행일`, 조문별은 `조문변경일`."""

    source_field: str
    """`version_time`을 읽어 온 필드명. 충돌 로그의 해석에 쓴다."""


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """무결성 검사 결과. 실패 사유가 하나라도 있으면 수집은 실패다.

    목적:
        건수 대조와 키 검사 결과를 실행 메타데이터에 남길 수 있는 형태로 담는다.

    구현 이유:
        `bool`이 아니라 보고서를 반환한다. **정상일 때도 무엇을 얼마나 검사했는지
        남겨야** "검사가 돌지 않아서 통과한 것"과 "검사가 돌아서 통과한 것"을
        구별할 수 있다 — 사건 3건 전부 "검사 로직이 잡은 것이 하나도 없다"였고,
        검사가 실제로 돌았는지 확인할 방법이 없으면 같은 일이 반복된다.

    트레이드오프:
        정상 경로에서도 객체를 만들므로 비용이 있다. 수집이 하루 1회이므로
        무시할 수 있다.

    엣지 케이스:
        - `revision_groups`는 **실패가 아니다.** 같은 법령ID에 다른 MST가 여러 개인
          것은 연혁이며 정상 데이터다. 값을 남기는 이유는 그것을 중복으로 오인해
          제거하는 코드가 생기면 이 숫자가 근거가 되기 때문이다.
    """

    spec_key: str
    total_count: int | None
    """`totalCnt`. 본문 계열은 None이며 건수 검사를 하지 않았다는 뜻이다."""

    item_count: int
    """`spec.item_path` 요소 수. 이력 계열에서는 **법령 수**다."""

    article_count: int
    """조문 요소 수. **완주 검사 대상이 아니다.** 참고용으로만 남긴다."""

    checked_keys: int
    """검사한 식별키 개수. 0이면 키 검사를 하지 않았다는 뜻이다."""

    failures: tuple[IntegrityFailure, ...]
    detail: str
    collisions: tuple[tuple[RecordKey, int], ...] = ()
    """충돌한 키와 그 횟수. 충돌 시 전체 필드를 로그에 남기기 위한 것이다."""

    revision_groups: tuple[tuple[str, int], ...] = ()
    """(법령ID, 서로 다른 MST 수). **연혁이며 정상이다.** 제거하지 않는다."""

    @property
    def ok(self) -> bool:
        """실패 사유가 하나도 없는가."""
        return not self.failures


def check_counts(
    spec: TargetSpec, total_count: int | None, items: Sequence[Element]
) -> tuple[tuple[IntegrityFailure, ...], str]:
    """완주 여부를 계열에 맞는 대상으로 대조한다.

    목적:
        `totalCnt`와 누적 수신 항목 수를 비교해 잘림과 중복 수신을 잡는다.

    구현 이유:
        **비교 대상이 계열마다 다르다.** 이력 계열의 `totalCnt`는 법령 수이므로
        `spec.item_path`(=`law`) 요소와 비교해야 하고, 조문(`article_path`)과
        비교하면 항상 어긋난다. 본문 계열은 `totalCnt`가 없어 검사 자체를 하지
        않는다. 이 분기를 호출부에 두면 새 target마다 실수할 자리가 생긴다.

        누락과 과잉을 **다른 사유로 구별한다.** 누락은 잘림이고 과잉은
        페이지네이션 버그이며 조치가 다르다. 그리고 누락만 검사하면 과잉을
        놓치는데, 과잉은 건수를 부풀려 "더 많이 받았으니 안전하다"는 착각을 준다.

    트레이드오프:
        `totalCnt`를 신뢰한다. 모듈 docstring의 트레이드오프 절 참조.

    엣지 케이스:
        - 본문 계열: 검사하지 않고 빈 실패 목록을 반환한다. `total_count`가
          None인 것이 정상이다.
        - `totalCnt`가 있는 계열에서 `total_count`가 None으로 들어오면 실패로
          본다 — 분류가 이미 막지만, 이 함수를 단독으로 쓰는 경로에서도 조용히
          통과하지 않게 한다.
        - `totalCnt=0`, 항목 0건: 완주다. 0건의 정당성은 판단하지 않는다.
    """
    if spec.family is ResponseFamily.DOCUMENT:
        return (), f"{spec.key}: 본문 계열이므로 완주 검사 대상이 아니다 (totalCnt 없음)"

    received = len(items)
    if total_count is None:
        return (
            (IntegrityFailure.SHORT_COUNT,),
            f"{spec.key}: {spec.family.value} 계열인데 totalCnt가 없다. 완주를 확인할 수 없으므로 "
            "실패로 본다 — 확인할 수 없는 것을 통과시키면 잘림이 정상으로 보인다",
        )

    counted = "법령 수" if spec.family is ResponseFamily.HISTORY else "항목 수"
    if received < total_count:
        return (
            (IntegrityFailure.SHORT_COUNT,),
            f"{spec.key}: 잘렸다. totalCnt={total_count}({counted})인데 {received}건만 받았다. "
            "응답에 잘렸다는 표시가 없으므로(edge-case #11) 이 대조가 유일한 검출 수단이다",
        )
    if received > total_count:
        return (
            (IntegrityFailure.OVER_COUNT,),
            f"{spec.key}: 과잉 수신. totalCnt={total_count}({counted})인데 {received}건을 받았다. "
            "페이지 경계에서 중복 수신했을 수 있다. 많이 받은 것도 실패다 — 건수가 부풀려진다",
        )
    return (), f"{spec.key}: 완주 확인. {counted} {received}/{total_count}"


def build_keys(spec: TargetSpec, items: Iterable[Element]) -> list[RecordKey]:
    """이력 항목에서 식별키를 만든다. 계열마다 시점 필드가 다르다.

    목적:
        병합 결과의 키 충돌을 검사할 수 있도록 키 목록을 만든다.

    구현 이유:
        키 구성이 spec에 선언되어 있고 이 함수는 그것을 읽을 뿐이다. 키를 코드에
        하드코딩하면 계열이 늘 때마다 이 함수를 고쳐야 하고, **어느 계열이 어떤
        키를 쓰는지가 두 곳에 흩어진다.**

        조문 요소를 항목(`<law>`) 아래에서 찾는다. 조문별 이력은 `<조문정보>`가
        단일이고 일자별은 `<jo>`가 반복이므로, `article_path`의 항목 이후 부분을
        상대 경로로 쓴다.

    트레이드오프:
        빈 필드를 만나도 예외를 던지지 않고 빈 문자열을 담는다. 검출은
        `check_keys`가 `MISSING_IDENTITY`로 한다 — **여기서 던지면 어느 행이
        몇 건 비었는지 집계하지 못하고 첫 행에서 멈춘다.**

    엣지 케이스:
        - 이력 계열이 아니면 빈 목록을 반환한다. 검색·본문 계열에는 조문 단위
          식별키가 없다.
        - `article_path`가 None인 이력 spec(`lsHstInf`)도 빈 목록이다. 그 target은
          조문 수준 정보가 없다 (§5.1).
    """
    if spec.family is not ResponseFamily.HISTORY:
        return []
    if not (spec.article_path and spec.identity_path):
        return []
    if not (spec.article_no_path and spec.version_time_path):
        return []

    _, _, relative_article_path = spec.article_path.partition("/")
    keys: list[RecordKey] = []
    for item in items:
        version_id = (item.findtext(spec.identity_path) or "").strip()
        for article in item.findall(relative_article_path):
            keys.append(
                RecordKey(
                    version_id=version_id,
                    article_no=(article.findtext(spec.article_no_path) or "").strip(),
                    version_time=(article.findtext(spec.version_time_path) or "").strip(),
                    source_field=spec.version_time_path,
                )
            )
    return keys


def check_keys(
    keys: Sequence[RecordKey],
) -> tuple[tuple[IntegrityFailure, ...], str, tuple[tuple[RecordKey, int], ...]]:
    """식별키 충돌과 빈 필드를 검사한다.

    목적:
        같은 키가 두 번 나타나면 **조용히 덮어쓰지 않고 실패로 만든다.**

    구현 이유:
        **"충돌 0건이므로 안전하다"가 아니라 "충돌 0건이지만 단언으로 감시한다."**
        캐시 365일 35,681행에서 충돌이 0건이었지만, **큰 표본에 없다는 것이 없다는
        뜻이 아니다** — `폐지제정`이 전수 35,681조문에 0건인데 픽스처 4,853조문에는
        19건이었다 (`law-api-spec.md` §5.4).

        **이 단언이 발화하는 날이 곧 키를 다시 검토할 날이다.** 그때는 충돌한 두
        행의 어느 필드가 다른지를 보고 키에 추가할지 판단한다. 그래서 충돌 시
        키 전체를 반환해 로그에 남긴다.

        빈 필드를 따로 잡는 이유는 **빈 키끼리는 서로 같아 보이기 때문이다.**
        모든 행의 키가 비면 전부 한 건으로 집계되고, 그것은 "충돌"이 아니라
        "누락"으로 나타난다 — 사건 1(dict 붕괴)과 같은 기전이다.

    트레이드오프:
        충돌한 키를 전부 반환하므로 대량 충돌 시 반환값이 커진다. 잘라내지 않은
        이유는, 잘라내면 로그가 "몇 건 더 있음"으로 끝나고 그 몇 건이 원인 규명에
        필요한 것일 수 있기 때문이다. 관측 범위에서 충돌은 0건이므로 실측 부담이 없다.

    엣지 케이스:
        - 빈 목록: 검사할 것이 없으므로 통과한다. 키가 0건인 것이 정상인지는
          건수 검사가 판단한다.
        - 빈 필드와 충돌이 동시에 발생하면 두 사유를 모두 반환한다. 빈 필드가
          충돌의 원인일 수 있으므로 한쪽만 보고하면 오진한다.
    """
    failures: list[IntegrityFailure] = []
    notes: list[str] = []

    incomplete = [
        key for key in keys if not (key.version_id and key.article_no and key.version_time)
    ]
    if incomplete:
        failures.append(IntegrityFailure.MISSING_IDENTITY)
        notes.append(
            f"식별키 필드가 빈 행 {len(incomplete)}건 (예: {incomplete[0]}). "
            "빈 키끼리는 서로 같아 보여 여러 행이 한 건으로 집계된다"
        )

    counts = Counter(keys)
    collisions = tuple((key, count) for key, count in counts.items() if count > 1)
    if collisions:
        failures.append(IntegrityFailure.KEY_COLLISION)
        notes.append(
            f"식별키 충돌 {sum(count - 1 for _, count in collisions)}건 / 고유 키 {len(counts)}개. "
            "조용히 덮어쓰지 않고 실패로 처리한다. **이 단언이 발화한 것은 키를 다시 "
            "검토할 신호다** — 충돌한 행의 어느 필드가 다른지 확인하라 (edge-case #18)"
        )

    if not failures:
        notes.append(f"식별키 충돌 없음. 고유 키 {len(counts)}개 / 행 {len(keys)}건")
    return tuple(failures), " / ".join(notes), collisions


def find_revision_groups(spec: TargetSpec, items: Sequence[Element]) -> tuple[tuple[str, int], ...]:
    """같은 법령ID에 서로 다른 MST가 몇 개인지 센다 — 이것은 실패가 아니다.

    목적:
        연혁(같은 법령ID, 다른 MST)의 규모를 실행 메타데이터에 남긴다.

    구현 이유:
        **연혁을 중복으로 오인해 제거하는 코드가 생기는 것을 막기 위한 숫자다.**
        `lschg_regdt20240719.xml`의 5건 중 법령ID `006612`가 2회 나타나며 MST가
        264383/230047로 다르다 — 제거하면 연혁이 사라진다 (edge-case #18).

        실패로 처리하지 않는 이유는 이것이 정상 데이터이기 때문이다. 그러나
        기록은 남긴다 — 조용히 지나가면 나중에 "연혁이 있었는가"를 확인할 수 없다.

    트레이드오프:
        `law_id_path`가 없는 spec(조문별 이력)에서는 셀 수 없어 빈 값을 반환한다.
        그 응답은 법령ID로 조회하므로 전 항목이 같은 법령이며, **서로 다른 MST는
        정의상 전부 연혁이다.** 따로 세는 의미가 없다.

    엣지 케이스:
        - `law_id_path`가 None: 빈 튜플. 위 트레이드오프 참조.
        - 법령ID가 빈 항목: 그룹에 넣지 않는다. 빈 값으로 묶으면 서로 무관한
          법령이 한 그룹이 된다.
    """
    if not (spec.law_id_path and spec.identity_path):
        return ()
    groups: dict[str, set[str]] = {}
    for item in items:
        law_id = (item.findtext(spec.law_id_path) or "").strip()
        version_id = (item.findtext(spec.identity_path) or "").strip()
        if not law_id:
            continue
        groups.setdefault(law_id, set()).add(version_id)
    return tuple(sorted((law_id, len(msts)) for law_id, msts in groups.items() if len(msts) > 1))


def check_integrity(
    spec: TargetSpec,
    total_count: int | None,
    items: Sequence[Element],
    articles: Sequence[Element],
) -> IntegrityReport:
    """건수 대조와 식별키 검사를 함께 수행해 보고서를 만든다.

    목적:
        수집 결과가 조용히 적어지지 않았음을 단언하는 단일 진입점.

    구현 이유:
        두 검사를 한 함수로 묶은 이유는 **호출부가 하나만 하고 넘어가는 것을
        막기 위해서다.** 건수만 맞고 키가 충돌하면 "전부 받았지만 일부가 같은
        것으로 보이는" 상태이며, 적재 시점에 조용히 덮어써진다.

    트레이드오프:
        본문 계열에서는 두 검사 모두 사실상 건너뛰므로 보고서가 거의 비어 있다.
        그래도 보고서를 만드는 이유는 **"검사를 하지 않았다"는 사실 자체가 기록될
        가치가 있기** 때문이다 — `total_count=None`, `checked_keys=0`이 그것을
        말한다.

    엣지 케이스:
        - 이력 계열이지만 `article_path`가 없는 spec(`lsHstInf`): 키 검사가 0건
          이며 건수 검사만 유효하다. 보고서의 `checked_keys=0`이 그 사실을 남긴다.
        - 실패 사유가 여러 개면 전부 담는다. 첫 사유에서 멈추면 나머지를 다음
          실행에서 다시 발견하게 된다.
    """
    count_failures, count_detail = check_counts(spec, total_count, items)
    keys = build_keys(spec, items)
    key_failures, key_detail, collisions = check_keys(keys)
    return IntegrityReport(
        spec_key=spec.key,
        total_count=total_count,
        item_count=len(items),
        article_count=len(articles),
        checked_keys=len(keys),
        failures=count_failures + key_failures,
        detail=f"{count_detail} / {key_detail}",
        collisions=collisions,
        revision_groups=find_revision_groups(spec, items),
    )
