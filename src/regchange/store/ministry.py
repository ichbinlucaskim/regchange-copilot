"""소관부처 코드/명칭 해결 — 위치 zip 금지, 시점 이전은 미상 (ADR-009).

목적:
    관측된 `소관부처코드`/`소관부처명` 에서 코드↔이름 대응을 만들고, 조문 문서의
    소관부처를 마스터에서 해결한다. 해결되지 않으면 조용히 넘기지 않고 미해결로
    기록한다.

구현 이유:
    두 가지 실측이 이 모듈의 형태를 정했다.

    **(1) 복수 나열 행을 위치로 zip 하면 34%가 틀린다.** 12개월 캐시에서 두 필드가
    쉼표로 여러 값을 나열한 행이 615건인데, 코드 개수와 이름 개수는 **항상 일치**하고
    (불일치 0건) 위치로 짝지으면 212건(34.5%)이 단일값 행에서 관측된 대응과 어긋난다.

        환경부와 그 소속기관 직제 (20250923)
          코드: 1741000,1482000
          이름: 기후에너지환경부,행정안전부      ← 순서가 반대다

    `농수산물 품질관리법`은 3개가 순환 이동해 있다. 길이가 항상 맞으므로 어떤 길이
    단언에도 걸리지 않고, 403건은 우연히 일치하므로 표본 점검도 통과한다. 그래서
    **복수 나열 행에서는 대응을 만들지 않는다.** 코드 목록과 이름 목록을 각각
    관측 사실로만 다루고, 짝짓기는 단일값 행에서만 한다.

    **(2) `소관부처명` 은 이미 현재값으로 평탄화되어 있다.** 같은 캐시에서 고유
    (코드, 이름) 쌍이 172개인데 고유 코드도 172개다 — **이름이 2개 이상인 코드가
    0개**다. 그런데 `법령구분명` 은 `환경부령`(~2025-09-30) / `기후에너지환경부령`
    (2025-10-01~) 로 이름 변경을 선명하게 보여준다. 즉 API 는 과거 행에도 오늘의
    부처명을 넣어 돌려준다. 본문 API 가 조문별 시행일을 문서 시행일로 평탄화하는
    것(ADR-005 근거 2)과 **같은 실패 기전**이다.

    따라서 관측된 이름을 과거로 소급하지 않는다. 관측 시점 이전을 물으면 이름을
    지어내지 않고 "미상"으로 답한다. 지어내면 ADR-009 가 막으려던 것을 정확히 하게
    된다 — 담당자가 2025년 9월 화면을 재현했는데 그때 없던 이름이 보인다.

트레이드오프:
    복수 나열 행 615건에서 대응을 하나도 얻지 못한다. 그중 403건은 위치 zip 이
    맞았을 것이므로 정보를 버리는 셈이다. 그 대신 212건을 조용히 잘못 배정하지
    않는다. 이 도메인에서 모르는 것과 틀린 것은 같은 값이 아니다.

    과거 시점 이름을 미상으로 답하므로 오래된 감사 재현에서 부처명 칸이 빈다.
    빈 칸은 담당자가 알아채지만 틀린 이름은 알아채지 못한다.

엣지 케이스:
    - 코드 개수 ≠ 이름 개수: 실측 0건이지만 관측되면 대응을 만들지 않는다.
      길이가 맞는 경우조차 짝짓지 않으므로 이 경우도 특별할 것이 없다.
    - 빈 문자열/공백: 값 없음으로 다룬다. 빈 문자열을 부처명으로 등재하지 않는다.
    - 코드는 있는데 마스터에 없음: `CODE_NOT_IN_MASTER`. 자동 등재하지 않는다.
    - 코드가 아예 없음: `CODE_MISSING`. 본문 API 는 `<소관부처 소관부처코드="...">`
      **XML 속성**으로 코드를 주므로, 태그만 읽는 파서는 이 값을 통째로 놓친다
      (ADR-001 의 조문키와 같은 함정).
    - 마스터의 이름과 관측된 이름이 다름: `NAME_MISMATCH`. 마스터가 더 오래된
      관측일 수 있으므로 자동으로 어느 쪽을 택하지 않는다.
    - 관측 시점 이전 조회: 이름을 반환하지 않는다 (`None`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

MULTI_VALUE_SEPARATOR = ","
"""법제처가 복수 부처를 나열할 때 쓰는 구분자. 12개월 캐시 615행에서 관측됐다."""


class UnresolvedReason(StrEnum):
    """소관부처가 해결되지 않은 이유. `ministry_unresolved.reason` 과 같은 값이다."""

    CODE_MISSING = "CODE_MISSING"
    """문서에 소관부처코드가 없다. 파서가 XML 속성을 놓쳤을 수 있다."""

    CODE_NOT_IN_MASTER = "CODE_NOT_IN_MASTER"
    """코드가 마스터에 없다. 새 조직이거나 마스터가 오래됐다 — 사람이 판단한다."""

    NAME_MISMATCH = "NAME_MISMATCH"
    """코드는 있으나 마스터의 이름과 관측된 이름이 다르다. 개편 신호일 수 있다."""


@dataclass(frozen=True, slots=True)
class MinistryObservation:
    """문서 하나에서 관측된 소관부처 원본 값."""

    code_field: str | None
    name_field: str | None

    @property
    def codes(self) -> tuple[str, ...]:
        """나열된 코드들. 이름과 짝짓지 않는다."""
        return _split(self.code_field)

    @property
    def names(self) -> tuple[str, ...]:
        """나열된 이름들. 코드와 짝짓지 않는다."""
        return _split(self.name_field)

    @property
    def is_single(self) -> bool:
        """코드와 이름이 각각 하나뿐인가. 대응을 만들 수 있는 유일한 경우다."""
        return len(self.codes) == 1 and len(self.names) == 1


@dataclass(frozen=True, slots=True)
class MinistryMasterRow:
    """마스터의 한 행. DB 의 `ministry_master` 열린 행에 대응한다."""

    org_code: str | None
    org_name: str
    valid_from: dt.date
    valid_until: dt.date | None = None

    def covers(self, at: dt.date) -> bool:
        """이 행이 `at` 시점에 유효한가. `valid_until` 은 열린 구간의 끝이다."""
        return self.valid_from <= at and (self.valid_until is None or at < self.valid_until)


@dataclass(frozen=True, slots=True)
class Resolution:
    """해결 결과. 실패를 값으로 표현한다 — 예외로 던지면 적재가 멈춘다."""

    org_code: str | None
    org_name: str | None
    reason: UnresolvedReason | None

    @property
    def resolved(self) -> bool:
        """마스터에서 해결됐는가."""
        return self.reason is None


def _split(field: str | None) -> tuple[str, ...]:
    """쉼표 나열을 자르고 공백만인 값을 버린다."""
    if field is None:
        return ()
    return tuple(part.strip() for part in field.split(MULTI_VALUE_SEPARATOR) if part.strip())


def extract_pair(observation: MinistryObservation) -> tuple[str, str] | None:
    """관측에서 (코드, 이름) 대응을 만든다. 단일값 행에서만 만든다.

    목적:
        마스터 초기 데이터와 문서 해결이 쓰는 유일한 짝짓기 경로를 한 곳에 둔다.

    구현 이유:
        짝짓기가 여러 곳에 흩어지면 그중 하나가 위치 zip 을 하게 되고, 그 하나는
        길이 검사에 걸리지 않으므로 리뷰에서도 테스트에서도 드러나지 않는다.
        경로를 하나만 두면 그 하나만 지키면 된다.

    트레이드오프:
        복수 나열 행의 정보를 버린다. 위 모듈 docstring 참조.

    엣지 케이스:
        - 복수 나열: `None`. 길이가 맞아도 짝짓지 않는다.
        - 코드나 이름이 비어 있음: `None`.
    """
    if not observation.is_single:
        return None
    return observation.codes[0], observation.names[0]


def resolve(
    observation: MinistryObservation,
    master: tuple[MinistryMasterRow, ...],
    *,
    at: dt.date,
) -> Resolution:
    """문서의 소관부처를 마스터에서 해결한다.

    목적:
        문서가 관측한 부처 코드/이름을 마스터의 그 시점 행과 대조해, 해결됐는지와
        해결되지 않았다면 왜인지를 값으로 돌려준다.

    구현 이유:
        실패를 예외가 아니라 값으로 표현한다. 미해결은 적재를 막는 사유가 아니라
        기록 대상이기 때문이다 — 부처명 하나 때문에 조문 전체가 안 들어가면
        조직 개편일마다 적재가 멈춘다(2025-10-01 하루에 법령 1,464건). 대신
        미해결은 별도 처분으로 세어져 적재 건수에 흡수되지 않는다.

    트레이드오프:
        호출자가 `Resolution.reason` 을 확인해야 한다. 확인하지 않으면 미해결이
        조용히 통과한다. 그 위험은 건수 단언이 잡는다 —
        `LOADED_UNRESOLVED` 를 세지 않으면 합이 어긋난다.

        복수 나열 문서는 항상 미해결이 된다. 12개월 캐시 기준 615행이 여기 걸린다.
        어느 부처를 기준으로 할지가 ADR-009 에서 미결정(TODO(verify))이므로,
        여기서 임의로 첫 번째를 고르지 않는다. 고르는 순간 그 선택이 관행이 되고
        아무도 결정한 적 없는 규칙이 생긴다.

    엣지 케이스:
        - 코드 없음 → `CODE_MISSING`
        - 복수 나열 → `CODE_NOT_IN_MASTER` 가 아니라, 짝을 만들 수 없으므로
          `CODE_MISSING` 과 구별해 `NAME_MISMATCH` 로도 두지 않는다.
          단일 코드가 아니면 대응이 정의되지 않으므로 `CODE_NOT_IN_MASTER` 로 둔다.
          (마스터에 "이 나열 조합"이 없다는 뜻이며, 실제로 없다.)
        - 마스터에 코드가 있으나 `at` 시점을 덮는 행이 없음 → `CODE_NOT_IN_MASTER`.
          관측 시점 이전을 물었을 때가 여기 해당하며, 이름을 지어내지 않는다.
        - 이름 불일치 → `NAME_MISMATCH`. 마스터 이름을 반환하되 미해결로 둔다.
    """
    pair = extract_pair(observation)
    if pair is None:
        if not observation.codes:
            return Resolution(org_code=None, org_name=None, reason=UnresolvedReason.CODE_MISSING)
        return Resolution(
            org_code=None,
            org_name=None,
            reason=UnresolvedReason.CODE_NOT_IN_MASTER,
        )

    code, observed_name = pair
    covering = [row for row in master if row.org_code == code and row.covers(at)]
    if not covering:
        return Resolution(
            org_code=code,
            org_name=None,
            reason=UnresolvedReason.CODE_NOT_IN_MASTER,
        )

    master_name = covering[0].org_name
    if master_name != observed_name:
        return Resolution(
            org_code=code,
            org_name=master_name,
            reason=UnresolvedReason.NAME_MISMATCH,
        )
    return Resolution(org_code=code, org_name=master_name, reason=None)


def name_at(master: tuple[MinistryMasterRow, ...], code: str, at: dt.date) -> str | None:
    """`at` 시점에 이 코드가 갖던 이름. 관측 이전이면 None.

    목적:
        과거 시점 재현에서 "그때 담당자가 보던 이름"을 돌려준다 (ADR-009).

    구현 이유:
        관측 이전 시점에 현재 이름을 돌려주면 재현이 재현이 아니게 된다.
        `None` 은 "이름이 없다"가 아니라 **"그 시점 이름을 우리는 모른다"**이며,
        화면은 그렇게 표시해야 한다.

    트레이드오프:
        오래된 시점의 재현 화면에 빈 칸이 는다. 빈 칸은 담당자가 알아채고 물어볼
        수 있지만, 틀린 이름은 그대로 믿는다.

    엣지 케이스:
        - 덮는 행이 여러 개: 첫 행을 쓴다. 마스터의 유니크 인덱스가 열린 행의
          중복을 막으므로 실제로는 발생하지 않으며, 발생하면 마스터가 깨진 것이다.
    """
    for row in master:
        if row.org_code == code and row.covers(at):
            return row.org_name
    return None
