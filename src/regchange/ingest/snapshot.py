"""수집한 응답을 **페이지별 원본 파일 + 매니페스트**로 보관한다. 병합하지 않는다.

목적:
    각 페이지를 법제처가 준 바이트 그대로 저장하고, 그 페이지들을 하나의 수집으로
    묶는 매니페스트를 함께 쓴다. 읽을 때는 매니페스트 순서대로 sha256을 검증하며
    되읽는다.

구현 이유:
    **병합하면 우리가 위조 증거를 만든다.** `<law>` 요소를 첫 응답 루트에 append
    하면 `totalCnt`와 실제 행 수가 어긋난 문서가 생기고, **그 어긋남은 지금까지
    잘린 응답을 판별하는 유일한 단서였다** (edge-case #11, 실증 픽스처 3종:
    jochg 28/20, lschg 71/5, search_eflaw 81/10). 감사에서 "그때 법제처가 준 것"을
    제시해야 하는데, 우리가 합친 문서는 그것이 아니다.

    **각 페이지는 그 자체로 완결된 적격 XML이며 위변조 없는 스냅샷이다.** 합치는
    순간 원본이 아니게 된다. 그리고 병합은 저장이 아니라 **읽기 시점의 문제**다 —
    소비자가 매니페스트를 보고 순서대로 읽으면 "합쳐진 문서"라는 애매한 물건이
    존재하지 않는다.

    이것은 ADR-002의 연장이다. `text_raw`를 원문 그대로 두고 `text_norm`을 파생으로
    둔 것과 같은 구조다 — **원본은 손대지 않고 가공은 파생으로 표현한다.**

    **매니페스트의 `params`는 허용 목록으로 만든다.** 거부 목록(OC만 제외)으로 두면
    나중에 추가되는 파라미터가 자동으로 따라 들어가고, 그중 민감한 것이 섞이면
    마스킹 목록을 갱신하지 않는 한 조용히 저장된다. **마스킹은 마지막 방어선이지
    첫 방어선이 아니다.**

트레이드오프:
    파일 수가 페이지 수만큼 늘고 매니페스트를 함께 관리해야 한다. 단일 파일의
    단순함을 포기한 대신 원본성을 얻었다 — 하루 최대 관측(법령 1,464건)이
    `display=100`에서 15개 파일이므로 상한이 있다.

    `directory`를 매니페스트에 명시한다. 키 인코딩 규칙으로 재계산할 수도 있지만,
    **인코딩이 바뀌면 과거 스냅샷을 못 읽게 된다.** 재계산 가능한 값을 굳이 저장하는
    비용보다 과거 데이터를 읽지 못하는 비용이 크다.

    이터레이터로 읽는다. 전체를 메모리에 올려 합치면 **병합을 피한 의미가 없어진다** —
    소비자가 결국 하나의 큰 문서를 들고 있게 되고, 그 문서는 다시 원본이 아니다.

엣지 케이스:
    - 허용 목록에 없는 파라미터가 들어오면 `SnapshotError`. `OC`는 목록에 없으므로
      **구조적으로 기록될 수 없다.**
    - 본문 계열은 `total_count`·`complete`를 **`null`로 둔다.** `false`로 두면
      "검사하지 않았다"와 "검사했고 실패했다"가 섞인다.
    - 같은 키 디렉터리에 다른 `params`의 매니페스트가 이미 있으면 실패다.
      6자리 해시 절단이므로 이론상 충돌이 가능하며, **매니페스트의 `params`가
      대조 수단이다.**
    - sha256 불일치·페이지 누락은 읽기 실패다. 조용히 건너뛰면 부분 데이터가
      정상으로 보인다.
    - 날짜와 시각을 섞지 않는다. `fetched_at`은 UTC ISO 8601, `regDt` 같은 법령
      날짜는 **법제처가 준 8자리 그대로** 둔다 — 시행일 `20260820`은 "KST 자정"이
      아니라 "그날부터 효력"이라는 법적 사실이며 타임존 변환 대상이 아니다.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from defusedxml.ElementTree import fromstring as _safe_fromstring

from regchange.adapters.storage import DocumentStore
from regchange.ingest.client import Collection
from regchange.ingest.integrity import IntegrityReport
from regchange.ingest.masking import Masker
from regchange.ingest.response import ResponseFamily, TargetSpec

_log = structlog.get_logger(__name__)

MANIFEST_FILENAME = "manifest.json"
"""매니페스트 파일명. 페이지 파일과 구별되도록 확장자를 다르게 뒀다."""

KEY_HASH_LENGTH = 6
"""디렉터리 키의 해시 접미사 길이.

**가독성과 충돌 확률의 절충이다.** 6자리는 24비트이므로 이론상 충돌이 가능하다.
길게 하면 디렉터리를 눈으로 훑기 어려워지고 — 이 시스템의 존재 이유가 소명
가능성이므로 사람이 읽을 수 있는 편이 낫다 — 짧게 하면 충돌이 잦아진다.

**충돌은 매니페스트 대조로 검출한다.** 같은 키 디렉터리에 다른 `params`의
매니페스트가 이미 있으면 덮어쓰지 않고 실패로 처리한다."""

RUN_ID_ENTROPY_LENGTH = 4
"""`run_id` 난수 접미사 길이. 같은 초에 두 실행이 시작될 수 있으므로 필요하다.

**난수의 목적은 유일성이지 예측 불가능성이 아니다.** 암호학적 난수일 필요는 없다.
`random.SystemRandom`을 쓰는 이유는 ruff S311을 `noqa` 없이 통과하기 위해서다
(ADR-012의 "예외를 만들지 않는다"와 같은 규칙)."""

_SAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._=-]")
"""디렉터리명에 허용하는 문자. 그 외는 `-`로 바꾼다.

통제 문자만 거부하는 방식을 쓰지 않은 이유: **파일시스템·OS에 따라 동작이 갈린다.**
macOS는 유니코드 정규화(NFD/NFC)를 하고 대소문자 구별도 설정에 따라 다르다.
실행 환경에 따라 결과가 달라지는 것은 재현성 요구와 충돌한다."""

_RUN_ID_PATTERN = re.compile(rf"^\d{{8}}T\d{{6}}Z-[0-9a-f]{{{RUN_ID_ENTROPY_LENGTH}}}$")
"""`run_id` 형식. UTC 기준이므로 **사전순 = 시간순**이며 어디서 돌려도 같다.

`build_manifest` 가 이 형식을 검사한다. 검사하는 이유는 형식이 갈리면 정렬이
깨지고, 정렬이 깨지면 "어느 실행이 먼저였는가"를 알 수 없게 되기 때문이다 —
`run_id` 의 존재 이유가 그 순서다."""


class SnapshotError(RuntimeError):
    """스냅샷 저장·조회가 실패했을 때 발생한다."""


def utc_now() -> datetime:
    """tz-aware UTC 현재 시각.

    목적:
        `datetime.now(UTC)` 호출을 한 곳으로 모은다.

    구현 이유:
        ruff DTZ가 naive datetime을 막는다(원칙 6). 호출 지점이 흩어지면 그중
        하나가 `datetime.now()`로 바뀌어도 눈에 띄지 않는다. 한 곳에 두면 검사가
        한 줄이다.

    트레이드오프:
        테스트에서 시각을 고정하려면 이 함수를 쓰지 않고 값을 주입해야 한다.
        그래서 이 모듈의 함수들은 `now`/`fetched_at`을 **인자로 받는다** — 시계를
        숨긴 함수는 시간 의존 동작을 검증할 수 없다.

    엣지 케이스:
        - 시스템 시계가 틀리면 잘못된 `run_id`가 만들어진다. 방어하지 않는다 —
          외부 시각 소스에 의존하는 비용이 이득보다 크고, `run_id`는 정렬용이지
          법적 시점이 아니다. 법적 시점은 법제처가 준 날짜다.
    """
    return datetime.now(UTC)


def new_run_id(now: datetime, *, entropy: str | None = None) -> str:
    """실행 식별자를 만든다. UTC 타임스탬프 + 난수 접미사.

    목적:
        한 수집 실행에 속한 모든 스냅샷을 하나로 묶는 식별자.

    구현 이유:
        **UTC를 쓴다.** `run_id`는 실행 순서를 정렬하는 데 쓰이고 정렬은 단조로워야
        한다. KST도 단조롭지만 다른 타임존 환경에서 돌리면 섞인다 — UTC는 어디서
        돌려도 같다. 그리고 오프셋이 파일명에 들어가면 정렬이 지역에 연결된다.

        난수를 붙이는 이유는 같은 초에 두 실행이 시작될 수 있기 때문이다.

    트레이드오프:
        운영자가 보는 시간(KST)과 다르므로 로그를 대조할 때 변환이 필요하다.
        **표시 시점에 변환하는 쪽을 택했다** — 저장된 값이 환경에 의존하지 않는
        것이 더 중요하다.

    엣지 케이스:
        - naive datetime을 주면 `SnapshotError`. 타임존 없는 시각은 어느 순간인지
          알 수 없으므로 조용히 UTC로 가정하지 않는다.
        - UTC가 아닌 tz-aware 시각은 UTC로 변환한다. 거부하지 않는 이유는 호출부가
          KST 시계를 갖고 있어도 결과가 같아야 하기 때문이다.
    """
    if now.tzinfo is None:
        raise SnapshotError(
            "naive datetime 으로 run_id 를 만들 수 없다. 타임존 없는 시각은 어느 순간인지 "
            "알 수 없으며, 조용히 UTC 로 가정하면 환경마다 다른 값이 된다 (원칙 6)"
        )
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = entropy if entropy is not None else _random_hex(RUN_ID_ENTROPY_LENGTH)
    return f"{stamp}-{suffix}"


def _random_hex(length: int) -> str:
    """유일성 확보용 16진 문자열. 암호학적 강도가 목적이 아니다."""
    system_random = random.SystemRandom()
    return "".join(system_random.choice("0123456789abcdef") for _ in range(length))


def semantic_params(spec: TargetSpec, params: Mapping[str, str]) -> dict[str, str]:
    """허용 목록에 있는 파라미터만 골라낸다. 목록 밖의 키는 예외다.

    목적:
        매니페스트와 디렉터리 키에 들어갈 값을 **구조적으로** 제한한다.

    구현 이유:
        **허용 목록이 거부 목록보다 안전하다.** "OC만 제외"로 두면 나중에 추가되는
        파라미터가 자동으로 따라 들어가고, 그중에 민감한 것이 섞이면 마스킹 목록을
        갱신하지 않는 한 조용히 저장된다. `OC`는 어떤 spec의 허용 목록에도 없으므로
        **애초에 들어갈 수 없다.**

        목록 밖의 키를 조용히 버리지 않고 예외를 던지는 이유: 그 키가 의미
        파라미터인지 수행 방식인지 우리가 모르기 때문이다. 의미 파라미터를 버리면
        서로 다른 요청이 같은 디렉터리로 붕괴하고, **붕괴는 조용하다.**

    트레이드오프:
        새 파라미터를 쓰려면 spec의 `optional_params`를 먼저 갱신해야 한다.
        번거롭지만, 그 갱신이 "이것은 요청의 일부인가 수행 방식인가"를 한 번
        생각하게 만든다 — 그 판단이 곧 디렉터리 키의 정의다.

    엣지 케이스:
        - `display`·`page`·`type`·`OC`: 목록에 없으므로 예외. 호출부는 의미
          파라미터만 넘긴다.
        - 값이 빈 문자열인 키: 포함한다. 빈 값도 요청의 일부이며, 필수 파라미터
          누락은 클라이언트가 요청 전에 잡는다.
    """
    allowed = spec.semantic_params
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise SnapshotError(
            f"{spec.key}: 허용 목록에 없는 파라미터 {unknown}. 매니페스트에 기록하지 "
            f"않는다 — 허용 목록은 {sorted(allowed)}다. 의미 파라미터라면 spec의 "
            "optional_params 를 갱신하고, 수행 방식이라면 넘기지 않는다"
        )
    return {name: params[name] for name in sorted(params)}


def encode_key(spec: TargetSpec, params: Mapping[str, str]) -> str:
    """요청 파라미터를 디렉터리명으로 바꾼다. 정규화 + 해시 접미사.

    목적:
        사람이 읽을 수 있으면서 서로 다른 요청이 절대 같은 이름이 되지 않는
        디렉터리명을 만든다.

    구현 이유:
        **정규화만으로는 서로 다른 요청이 같은 디렉터리로 붕괴한다.** 허용 문자
        밖을 `-`로 바꾸면 `query=가나`와 `query=다라`가 둘 다 `query--`가 된다.
        그리고 붕괴는 조용하다 — 나중에 덮어쓰기가 일어나도 예외가 없다. 이
        저장소가 반복해서 잡아온 실패 유형(dict 붕괴, 조용한 누락)과 같은 구조다.

        **해시 입력은 정규화 전 원본 값이다.** 정규화 후를 해시하면 붕괴한 두
        요청이 같은 해시를 갖게 되어 방어가 무의미해진다.

        키 정렬은 사전순이다. 딕셔너리 순회 순서에 의존하면 같은 요청이 실행마다
        다른 해시를 가질 수 있다.

        해시만 쓰지 않는 이유: 디렉터리를 눈으로 훑을 수 없다. 감사 대응과
        디버깅에서 `regDt-20260113-8f2a1c`는 무엇인지 바로 보이지만 `b71f52`는
        아니다.

    트레이드오프:
        6자리 절단이라 이론상 충돌이 가능하다. `KEY_HASH_LENGTH` docstring 참조 —
        충돌은 매니페스트의 `params` 대조로 검출한다.

    엣지 케이스:
        - 허용 목록 밖의 키: `semantic_params`가 예외를 던진다.
        - `display`는 해시 입력에서 제외된다(허용 목록에 없으므로). 같은 논리적
          요청이 `display` 값 때문에 다른 디렉터리로 갈라지면 안 된다.
        - 파라미터가 0건: 이론상 가능하지만 모든 spec에 필수 파라미터가 있어
          실제로는 발생하지 않는다. 발생하면 해시만 남는다.
    """
    selected = semantic_params(spec, params)
    serialized = "&".join(f"{name}={value}" for name, value in selected.items())
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:KEY_HASH_LENGTH]
    readable = "_".join(f"{name}-{value}" for name, value in selected.items())
    return f"{_SAFE_KEY_CHARS.sub('-', readable)}-{digest}"


@dataclass(frozen=True, slots=True)
class PageRecord:
    """페이지 파일 하나의 기록."""

    file: str
    sha256: str
    items: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """한 수집의 페이지들을 묶는 매니페스트.

    목적:
        "무엇을 언제 어떤 요청으로 받았고, 완주했는가"를 페이지 목록과 함께 남긴다.

    구현 이유:
        `complete`와 `total_count`를 `bool | None` / `int | None`으로 뒀다.
        **본문 계열은 `null`이며 `false`가 아니다** — `false`로 두면 "검사하지
        않았다"와 "검사했고 실패했다"가 섞이고, 그것이 이 저장소가 반복해서 겪은
        혼동(0건과 실패를 같은 값으로 표현)의 같은 형태다.

        `count_target`을 함께 남기는 이유는 재계산 판별 원칙 3의 적용이다 —
        **재계산 대상이 무엇인지 명시한다.** 이력 계열의 `totalCnt`는 법령 수이지
        조문 수가 아니므로, 나중에 이 숫자를 해석하려면 무엇을 세었는지 알아야 한다.

    트레이드오프:
        JSON으로 직렬화하므로 타입 정보가 약해진다. pydantic 대신 dataclass +
        명시적 직렬화를 쓴 이유는 `config/corpus.py`와 같다 — 스키마가 얕고
        검증 규칙이 도메인 특유하다.

    엣지 케이스:
        - `unknown_enum_values`가 비어 있지 않아도 수집은 성공이다. 기록 대상이다.
        - `key_conflicts`는 성공한 수집에서 항상 0이다. 필드를 남기는 이유는
          **"검사했고 0이었다"를 기록하기 위해서다** — 검사가 돌지 않아서 0인 것과
          구별된다.
    """

    run_id: str
    fetched_at: str
    """UTC ISO 8601. 법령 날짜(`regDt` 등)는 변환하지 않고 `params`에 그대로 둔다."""

    target: str
    endpoint: str
    series: str
    """계열 이름(`SEARCH`/`HISTORY`/`DOCUMENT`)."""

    directory: str
    """페이지 파일들이 있는 접두사. 인코딩이 바뀌어도 과거 스냅샷을 읽기 위해 저장한다."""

    params: Mapping[str, str]
    display: int | None
    """이 수집에 쓴 `display`. 본문 계열은 None이다.

    기록하는 이유: `PAGE_SIZE` 상수를 나중에 바꾸면 지난 스냅샷이 어떤 페이지
    크기로 받은 것인지 사후에 알 수 없다. 그리고 "마지막이 아닌데 덜 찬 페이지"
    검출 로직이 이 값에 의존하므로, 완주 판정을 재해석하려면 필요하다."""

    pages: tuple[PageRecord, ...]
    total_count: int | None
    received: int
    complete: bool | None
    count_target: str
    key_conflicts: int
    unknown_enum_values: tuple[Mapping[str, object], ...]
    retry_count: int
    request_count: int

    def to_json(self) -> str:
        """매니페스트를 JSON 문자열로 만든다. 한글을 이스케이프하지 않는다."""
        payload: dict[str, object] = {
            "run_id": self.run_id,
            "fetched_at": self.fetched_at,
            "target": self.target,
            "endpoint": self.endpoint,
            "series": self.series,
            "directory": self.directory,
            "params": dict(self.params),
            "display": self.display,
            "pages": [
                {"file": page.file, "sha256": page.sha256, "items": page.items}
                for page in self.pages
            ],
            "total_count": self.total_count,
            "received": self.received,
            "complete": self.complete,
            "count_target": self.count_target,
            "key_conflicts": self.key_conflicts,
            "unknown_enum_values": [dict(entry) for entry in self.unknown_enum_values],
            "retry_count": self.retry_count,
            "request_count": self.request_count,
        }
        return json.dumps(payload, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        """JSON 문자열에서 매니페스트를 복원한다.

        엣지 케이스:
            - 필드가 빠져 있으면 `KeyError`가 전파된다. 기본값으로 메우지 않는다 —
              메우면 "검사하지 않았다"가 "검사했다"로 위장한다.
        """
        data = json.loads(raw)
        return cls(
            run_id=data["run_id"],
            fetched_at=data["fetched_at"],
            target=data["target"],
            endpoint=data["endpoint"],
            series=data["series"],
            directory=data["directory"],
            params=data["params"],
            display=data["display"],
            pages=tuple(
                PageRecord(file=page["file"], sha256=page["sha256"], items=page["items"])
                for page in data["pages"]
            ),
            total_count=data["total_count"],
            received=data["received"],
            complete=data["complete"],
            count_target=data["count_target"],
            key_conflicts=data["key_conflicts"],
            unknown_enum_values=tuple(data["unknown_enum_values"]),
            retry_count=data["retry_count"],
            request_count=data["request_count"],
        )


def count_target_of(spec: TargetSpec) -> str:
    """완주 검사가 무엇을 세었는지 이름으로 돌려준다.

    목적:
        매니페스트에 "재계산 대상"을 명시한다 (판별 원칙 3).

    구현 이유:
        숫자만 남기면 나중에 그것이 법령 수인지 조문 수인지 알 수 없다. 이력
        계열의 `totalCnt=83`과 조문 286건을 혼동하면 완주 판정이 뒤집힌다.

    트레이드오프:
        문자열이라 타입 안전하지 않다. enum으로 만들 수도 있지만 이 값은 매니페스트
        기록 전용이며 코드가 분기하지 않으므로, JSON에 그대로 나가는 문자열이 낫다.

    엣지 케이스:
        - 본문 계열: `"none"`. 검사하지 않았다는 뜻이며 `complete`도 None이다.
    """
    if spec.family is ResponseFamily.DOCUMENT:
        return "none"
    if spec.family is ResponseFamily.HISTORY:
        return "law_elements"
    return "item_elements"


def build_manifest(
    collection: Collection,
    *,
    run_id: str,
    fetched_at: datetime,
    params: Mapping[str, str],
    display: int | None,
) -> Manifest:
    """수집 결과에서 매니페스트를 만든다. 페이지별 sha256을 계산한다.

    목적:
        저장 직전에 무엇을 저장하는지 기술하는 값을 만든다.

    구현 이유:
        `Collection.bodies`(마스킹된 본문)에서 해시를 계산한다. 원본 바이트가
        아니라 마스킹된 바이트를 해시하는 이유: **저장되는 것이 마스킹된 본문이고,
        해시는 저장된 것의 무결성을 증명해야 한다.** 원본을 해시하면 파일과 해시가
        영원히 불일치한다.

        `complete`를 계열에서 파생시킨다. 본문 계열은 None이며, 그 외에는 무결성
        보고서의 건수 검사 결과다.

    트레이드오프:
        본문을 UTF-8로 인코딩해 해시하므로 인코딩 왕복이 한 번 있다. 저장할 때도
        같은 인코딩을 쓰므로 결과는 일치하며, 비용은 2.1MB에서 수 ms다.

    엣지 케이스:
        - 허용 목록 밖의 파라미터: `semantic_params`가 예외를 던진다.
        - naive `fetched_at`: `SnapshotError`. 원칙 6상 시점은 타임존을 가져야 한다.
        - 페이지가 0개: 발생하지 않는다. 성공한 수집은 최소 1페이지를 갖는다.
    """
    if fetched_at.tzinfo is None:
        raise SnapshotError("fetched_at 에 타임존이 없다. 어느 순간인지 알 수 없다 (원칙 6)")
    if not _RUN_ID_PATTERN.match(run_id):
        raise SnapshotError(
            f"run_id 형식이 아니다: {run_id!r}. `new_run_id()` 로 만든다 — 형식이 갈리면 "
            "사전순 정렬이 시간순과 어긋나고, 그러면 어느 실행이 먼저였는지 알 수 없다"
        )

    spec = collection.spec
    selected = semantic_params(spec, params)
    directory = f"{run_id}/{spec.target}/{encode_key(spec, params)}"
    report: IntegrityReport = collection.report

    pages: list[PageRecord] = []
    for index, body in enumerate(collection.bodies, start=1):
        encoded = body.encode("utf-8")
        pages.append(
            PageRecord(
                file=f"page-{index:03d}.xml",
                sha256=hashlib.sha256(encoded).hexdigest(),
                items=_count_items(body, spec),
            )
        )

    is_document = spec.family is ResponseFamily.DOCUMENT
    return Manifest(
        run_id=run_id,
        fetched_at=fetched_at.astimezone(UTC).isoformat(),
        target=spec.target,
        endpoint=spec.endpoint,
        series=spec.family.value,
        directory=directory,
        params=selected,
        display=None if is_document else display,
        pages=tuple(pages),
        total_count=report.total_count,
        received=report.item_count,
        complete=None if is_document else report.ok,
        count_target=count_target_of(spec),
        key_conflicts=sum(count - 1 for _, count in report.collisions),
        unknown_enum_values=tuple(
            {"field": field, "values": list(values)}
            for field, values in sorted(collection.unknown_enum_values.items())
        ),
        retry_count=collection.stats.retries,
        request_count=collection.stats.requests,
    )


def _count_items(body: str, spec: TargetSpec) -> int:
    """페이지 하나의 항목 수를 센다. 매니페스트 기록용이다."""
    root = _safe_fromstring(body)
    return len(root.findall(spec.item_path))


async def write_snapshot(
    store: DocumentStore,
    collection: Collection,
    *,
    run_id: str,
    fetched_at: datetime,
    params: Mapping[str, str],
    display: int | None,
    masker: Masker,
) -> Manifest:
    """페이지들을 원본 그대로 저장하고 매니페스트를 함께 쓴다.

    목적:
        수집 결과를 감사에 제시할 수 있는 형태로 보관한다.

    구현 이유:
        **페이지를 먼저 쓰고 매니페스트를 마지막에 쓴다.** 매니페스트가 존재하면
        그 안의 페이지가 모두 있다는 뜻이 되고, 중간에 중단되면 매니페스트가
        없으므로 **불완전한 수집이 완전한 것으로 보이지 않는다.**

        매니페스트도 마스킹 경로를 통과시킨다. `params`가 허용 목록으로 이미
        제한되지만, **마스킹은 마지막 방어선**이므로 둘 다 둔다.

    트레이드오프:
        페이지를 쓴 뒤 매니페스트 쓰기가 실패하면 고아 페이지가 남는다. 정리하지
        않는 이유는 **삭제 경로를 만들지 않기로** 했기 때문이다(원칙 6, 인터페이스
        규약). 고아 페이지는 매니페스트가 없어 읽히지 않으므로 무해하다.

    엣지 케이스:
        - 같은 키 디렉터리에 매니페스트가 이미 있으면 `SnapshotError`. 해시 충돌
          또는 같은 요청 재수집이며, `params`를 대조해 어느 쪽인지 알린다.
        - 페이지 파일이 이미 있으면 저장소가 거부한다(덮어쓰기 금지). 그 예외를
          그대로 전파한다.
        - 마스킹 실패: `MaskingError`가 전파된다. 저장을 중단시킨다.
    """
    manifest = build_manifest(
        collection,
        run_id=run_id,
        fetched_at=fetched_at,
        params=params,
        display=display,
    )
    manifest_key = f"{manifest.directory}/{MANIFEST_FILENAME}"

    existing = await _read_manifest_if_present(store, manifest_key)
    if existing is not None:
        raise SnapshotError(
            f"이미 매니페스트가 있는 디렉터리다: {manifest.directory}. "
            f"기존 params={dict(existing.params)} / 새 params={dict(manifest.params)}. "
            "params 가 다르면 해시 충돌이며(6자리 절단), 같으면 같은 요청의 재수집이다. "
            "어느 쪽이든 덮어쓰지 않는다"
        )

    for page, body in zip(manifest.pages, collection.bodies, strict=True):
        await store.put(f"{manifest.directory}/{page.file}", masker.mask(body).encode("utf-8"))

    await store.put(manifest_key, masker.mask(manifest.to_json()).encode("utf-8"))
    _log.info(
        "snapshot.written",
        run_id=run_id,
        directory=manifest.directory,
        pages=len(manifest.pages),
        received=manifest.received,
        total_count=manifest.total_count,
        complete=manifest.complete,
        count_target=manifest.count_target,
        retry_count=manifest.retry_count,
    )
    return manifest


async def _read_manifest_if_present(store: DocumentStore, key: str) -> Manifest | None:
    """매니페스트가 있으면 읽고 없으면 None. 존재 확인 전용이다."""
    try:
        raw = await store.get(key)
    except Exception:
        return None
    return Manifest.from_json(raw.decode("utf-8"))


async def read_pages(store: DocumentStore, manifest: Manifest) -> AsyncIterator[bytes]:
    """매니페스트 순서대로 페이지를 읽어 하나씩 내놓는다. sha256을 검증한다.

    목적:
        저장한 스냅샷을 원본 그대로, 순서대로, 무결성 확인과 함께 되읽는다.

    구현 이유:
        **이터레이터로 둔 이유는 병합을 피한 의미를 지키기 위해서다.** 전체를
        메모리에 올려 합치면 소비자가 결국 하나의 큰 문서를 들고 있게 되고, 그
        문서는 다시 원본이 아니다 — `totalCnt`와 행 수가 어긋난 물건이 만들어진다.
        한 페이지씩 내놓으면 소비자가 각 페이지를 적격 XML로 파싱한다.

        sha256을 검증하는 이유는 파일이 바뀌었는지 확인하는 것이고, 그것이 감사
        계층에서 "시스템이 본 원문"의 무결성을 증명하는 근거가 된다 (ADR-007).

    트레이드오프:
        페이지마다 해시를 계산하므로 읽기 비용이 늘어난다. 검증 없는 빠른 경로를
        두지 않은 이유: **선택 가능한 검증은 결국 꺼진다.** 스냅샷 읽기는 감사와
        재처리에서만 일어나므로 빈도가 낮다.

    엣지 케이스:
        - 페이지 파일이 없으면 `SnapshotError`. 매니페스트에 있는데 파일이 없는
          것은 부분 손실이며, 건너뛰면 **부분 데이터가 정상으로 보인다.**
        - sha256 불일치: `SnapshotError`. 내용이 바뀐 파일을 원문이라 부를 수 없다.
        - 페이지가 0개인 매니페스트: 아무것도 내놓지 않는다. 그런 매니페스트는
          만들어지지 않지만, 만들어졌다면 그 사실이 `pages=[]`로 드러난다.
    """
    for page in manifest.pages:
        key = f"{manifest.directory}/{page.file}"
        try:
            body = await store.get(key)
        except Exception as exc:
            raise SnapshotError(
                f"매니페스트에 있는 페이지 파일이 없다: {key}. 건너뛰지 않는다 — "
                "건너뛰면 부분 데이터가 정상으로 보인다"
            ) from exc
        actual = hashlib.sha256(body).hexdigest()
        if actual != page.sha256:
            raise SnapshotError(
                f"sha256 불일치: {key}. 기대 {page.sha256}, 실제 {actual}. "
                "내용이 바뀐 파일을 원문이라 부를 수 없다 (ADR-007)"
            )
        yield body
