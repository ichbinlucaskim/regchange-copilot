"""소관부처 해결 — 위치 zip 금지와 시점 소급 금지를 고정한다.

이 테스트가 존재하는 이유: 두 함정이 **아무 오류도 내지 않는다.**
(1) 코드/이름을 위치로 짝지으면 12개월 실측 615행 중 212행이 조용히 어긋난다.
    길이는 항상 맞으므로 어떤 길이 단언에도 걸리지 않는다.
(2) 관측된 이름을 과거로 소급하면 그 시점에 없던 부처명이 재현 화면에 나온다.
둘 다 값이 채워지고 합계가 맞으므로, 테스트로 고정하지 않으면 되돌아온다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from regchange.store.ministry import (
    MinistryMasterRow,
    MinistryObservation,
    UnresolvedReason,
    extract_pair,
    name_at,
    resolve,
)

MASTER = (
    MinistryMasterRow(
        org_code="1160100",
        org_name="금융위원회",
        valid_from=dt.date(2026, 8, 11),
        valid_until=None,
    ),
    MinistryMasterRow(
        org_code="1482000",
        org_name="기후에너지환경부",
        valid_from=dt.date(2026, 8, 11),
        valid_until=None,
    ),
)

OBSERVED = dt.date(2026, 8, 11)


def test_single_value_row_yields_pair() -> None:
    """단일값 행에서만 대응이 만들어진다."""
    observation = MinistryObservation(code_field="1160100", name_field="금융위원회")
    assert extract_pair(observation) == ("1160100", "금융위원회")


@pytest.mark.parametrize(
    ("codes", "names"),
    [
        # 실측: 환경부와 그 소속기관 직제(20250923). 순서가 반대다.
        ("1741000,1482000", "기후에너지환경부,행정안전부"),
        # 실측: 농수산물 품질관리법(20250826). 3개가 순환 이동해 있다.
        ("1471000,1543000,1192000", "농림축산식품부,해양수산부,식품의약품안전처"),
        # 실측: 관세청과 그 소속기관 직제(20250923).
        ("1741000,1220000", "관세청,행정안전부"),
    ],
)
def test_multi_value_row_never_yields_pair(codes: str, names: str) -> None:
    """복수 나열 행은 길이가 맞아도 짝짓지 않는다.

    아래 세 건은 전부 12개월 캐시의 실제 행이며, 위치 zip 이 틀리는 실증이다.
    길이 불일치는 615행 전체에서 0건이므로 길이 검사는 이 함정을 잡지 못한다.
    """
    observation = MinistryObservation(code_field=codes, name_field=names)
    assert len(observation.codes) == len(observation.names), "실측: 길이는 항상 맞는다"
    assert extract_pair(observation) is None


def test_resolves_when_code_in_master() -> None:
    """마스터에 있는 코드는 해결된다."""
    observation = MinistryObservation(code_field="1160100", name_field="금융위원회")
    resolution = resolve(observation, MASTER, at=OBSERVED)
    assert resolution.resolved
    assert resolution.org_name == "금융위원회"


def test_unknown_code_is_unresolved_not_auto_registered() -> None:
    """모르는 코드는 미해결이다. 자동 등재하지 않는다 (ADR-009)."""
    observation = MinistryObservation(code_field="9999999", name_field="가상부")
    resolution = resolve(observation, MASTER, at=OBSERVED)
    assert not resolution.resolved
    assert resolution.reason is UnresolvedReason.CODE_NOT_IN_MASTER


def test_missing_code_is_distinguished_from_unknown_code() -> None:
    """코드가 없는 것과 코드를 모르는 것을 구별한다.

    코드 결측은 파서가 XML 속성을 놓쳤다는 신호이고, 코드 미등재는 조직 개편
    신호다. 같은 값으로 뭉개면 파서 회귀가 운영 이슈로 보인다.
    """
    resolution = resolve(
        MinistryObservation(code_field=None, name_field="금융위원회"), MASTER, at=OBSERVED
    )
    assert resolution.reason is UnresolvedReason.CODE_MISSING


def test_name_mismatch_does_not_silently_pick_a_side() -> None:
    """마스터 이름과 관측 이름이 다르면 미해결이다. 어느 쪽도 자동으로 택하지 않는다."""
    observation = MinistryObservation(code_field="1482000", name_field="환경부")
    resolution = resolve(observation, MASTER, at=OBSERVED)
    assert resolution.reason is UnresolvedReason.NAME_MISMATCH
    assert resolution.org_name == "기후에너지환경부"


def test_name_is_not_backdated_before_observation() -> None:
    """관측 시점 이전을 물으면 이름을 지어내지 않는다.

    이것이 ADR-009 가 막으려던 것이다 — 2025년 9월 화면을 재현했는데 그때 없던
    이름(기후에너지환경부)이 보이면 담당자는 자기 판단을 검증할 수 없다.
    """
    assert name_at(MASTER, "1482000", dt.date(2025, 9, 30)) is None
    assert name_at(MASTER, "1482000", OBSERVED) == "기후에너지환경부"


def test_resolution_before_observation_is_unresolved() -> None:
    """관측 이전 시점 해결은 미해결이다. 현재 이름으로 채우지 않는다."""
    observation = MinistryObservation(code_field="1482000", name_field="기후에너지환경부")
    resolution = resolve(observation, MASTER, at=dt.date(2025, 9, 30))
    assert not resolution.resolved
    assert resolution.org_name is None
