"""골든셋 시나리오 YAML의 스키마와 난이도 분포를 검사한다.

이 테스트가 존재하는 이유: 골든셋은 **평가의 정답지**다. 스키마가 조용히 어긋나면
러너가 일부 필드를 못 읽고, 그 결과는 "지표가 낮다"가 아니라 "지표가 잘못 계산됐다"로
나타난다. 사람이 케이스를 추가·수정할 때 형식을 강제할 수단이 필요하다.

난이도 분포까지 검사하는 이유: 분포 요구(EASY 3 / MEDIUM 4 / HARD 3 / DECOY 5 /
EMPTY 2~3)는 이 평가셋이 무엇을 측정할 수 있는지를 결정한다. 케이스를 지우거나 고치다
EMPTY가 1건이 되면 "모른다고 말하는 기능"의 측정이 사라지는데, 그 사라짐은 지표에
드러나지 않는다 — 남은 케이스의 점수만 보이기 때문이다.

난이도는 YAML에 저장하지 않고 여기서 파생한다. 같은 사실을 두 곳에 적으면 어긋나고,
어긋났을 때 어느 쪽이 맞는지 알 수 없다. 파생 규칙은
`evals/datasets/golden/README.md` §3에 문서화돼 있다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from regchange.diff.models import ChangeType

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "golden"

CASE_PATHS = sorted(GOLDEN_DIR.glob("case-*.yaml"))

VALID_OUTCOMES = frozenset({"IMPACT", "NO_IMPACT", "INSUFFICIENT_EVIDENCE"})
VALID_RISKS = frozenset({"HIGH", "MEDIUM", "LOW"})
VALID_DIFFICULTIES = frozenset({"EASY", "MEDIUM"})
"""문단 단위 난이도. HARD 는 문단이 아니라 **케이스**의 성질이라 여기 없다 —
한 개정이 여러 문서에 걸치는 것을 HARD 라 부르기 때문이다."""

VALID_REVISION_KINDS = frozenset({"일부개정", "타법개정", "제정", "전부개정", "일괄개정"})
"""`제개정구분명`의 관측 어휘 5종. 근거: docs/domain-selection/amendment-frequency.md D-2."""

DOC_ARTICLE_COUNTS = {
    "ISP-POL-001": 18,
    "ISP-GUIDE-002": 44,
    "ISP-GUIDE-003": 40,
    "ISP-PROC-001": 27,
    "ISP-PROC-002": 23,
}
"""각 사내 문서의 조문 수. 근거: `docs/09-corpus-design.md` §3의 목차.

여기 둔 이유는 **시나리오가 존재하지 않는 조를 가리키는 것을 막기 위해서**다.
2-B가 문서를 쓰기 전까지 조항은 명세로만 존재하므로, 조 번호가 문서 크기를 넘어도
아무도 알아채지 못한다. 문서가 만들어진 뒤에 발견하면 시나리오를 다시 설계해야 한다."""

KNOWN_DOC_IDS = frozenset(DOC_ARTICLE_COUNTS)

SOURCE_REQUIRED = (
    "law_id",
    "law_name",
    "mst",
    "promulgation_date",
    "promulgation_no",
    "revision_kind",
    "effective_date",
    "article_path",
    "change_type",
    "after",
    "summary",
    "evidence",
)

MIN_EASY_IMPACTS = 3
MIN_MEDIUM_IMPACTS = 4
MIN_HARD_CASES = 3
MIN_DECOY_CASES = 5
MIN_EMPTY_CASES = 2
MAX_EMPTY_CASES = 12
"""`evals/datasets/golden/README.md` §3의 요구. 상한이 있는 것은 EMPTY 뿐이다 —
EMPTY 가 너무 많으면 "항상 모른다"고 답하는 시스템이 높은 점수를 받는다.

**2026-08-24 에 3 → 12 로 갱신했다.** EMPTY(INSUFFICIENT_EVIDENCE) 정답이 11건이고
여유 1을 둬 12로 한다.

내역: 영역무관 5 + 신설 2 + 부분무관 3 + DECOY만 1 = 11.
**이 합은 `DECLARED_TYPE_COUNTS` 의 EMPTY 계열 4종 합과 같아야 한다** — 두 곳에 적힌
같은 사실이므로 어긋나면 둘 중 하나가 틀린 것이고, `test_type_counts_sum_to_total` 과
`test_difficulty_distribution` 이 각각 다른 경로로 센다.

상한의 목적은 "항상 모른다"고 답하는 시스템이 만점을 받는 것을 막는 것이다.
42건 중 12건(28.6%)이면 나머지 30건에서 무언가를 찾아야 하므로 그 압력이 유지된다.

**3 은 15건 규모의 값이었다.** 골든셋이 다시 늘면 이 값도 함께 본다 — 상한을 그대로
두면 "영향 없음을 절반 이상으로" 라는 확장 목표와 정면으로 충돌한다."""

DECLARED_TOTAL_CASES = 42
"""선언된 총 건수. **이 숫자가 이 파일의 기준점이다.**

42 인 이유: 기존 15 + 신규 27. 처음 계획은 43 이었고, 그것이 §7 에서 드러난 어긋남이다 —
영향 없음이 23 에서 22 로 줄었는데(277569 원천 상한 위반을 고치며 타법개정 일반을
8 → 7 로 줄였다) 총계 43 을 그대로 두었다. **손으로 센 결과였다.**

같은 실수가 이 저장소에서 처음이 아니다(부처 코드 172 → 62, 이동 표기 12 → 15).
그래서 이제 총계·유형별 합·비율·원천 상한을 전부 테스트가 센다."""

DECLARED_NO_IMPACT_CASES = 22
"""정답이 「영향 없음」인 건수 = NO_IMPACT + INSUFFICIENT_EVIDENCE. 42건의 52.4%."""

DECLARED_IMPACT_CASES = 20
"""정답이 IMPACT 인 건수. **21 로 늘리지 않는다** — 원천당 신규 3건 상한 안에서
20 이 이미 8개 원천을 쓴다."""

DECLARED_SOURCE_COUNT = 19
"""서로 다른 원천(MST)의 수. **손으로 세지 않는다** — 기존 8개와 신규 11개의 합집합이며
`test_source_count_matches_declaration` 이 케이스 파일에서 직접 센다."""

MIN_NO_IMPACT_RATIO = 0.50
"""「영향 없음」이 정답인 비율의 하한. 근거: `README.md` §3 의 확장 목표.

실제 운영 분포에서 타법개정만으로 이미 56.2% 다
(`docs/domain-selection/amendment-frequency.md` D-2·D-3)."""

MAX_NEW_IMPACT_PER_SOURCE = 3
MAX_NEW_NO_IMPACT_PER_SOURCE = 4
"""**신규** 케이스의 원천당 상한. 기존 15건에는 걸지 않는다.

기존에 걸 수 없는 이유는 사실이 이미 그렇기 때문이다 — MST=285199 는 IMPACT 5건,
MST=283839 는 4건이며 둘 다 상한을 넘는다. 15건 규모에서는 원천이 8개뿐이라 한 원천에
몰리는 것이 불가피했다. **상한은 확장분이 같은 편중을 반복하지 않게 하려는 것**이므로
확장분에만 건다. 기존을 소급해 고치면 (가)·(나) 시나리오가 깨진다.

값의 근거: 원천 하나가 틀리면 그 원천의 케이스가 함께 틀린다. 3~4건이면 한 원천의
실패가 유형별 정확도를 뒤집지 못한다."""

EXISTING_IDS = frozenset(f"case-{n:03d}" for n in range(1, 16))
"""15건 규모 시점에 이미 있던 케이스. **역사적 사실이지 케이스의 성질이 아니므로**
YAML 에 적지 않고 여기 둔다."""

VALID_CASE_TYPES = frozenset(
    {
        "IMPACT",
        "OTHER_LAW_PLAIN",
        "OTHER_LAW_B1_HIGH",
        "EMPTY_OUT_OF_SCOPE",
        "EMPTY_NEW_PROVISION",
        "EMPTY_PART_UNRELATED",
        "DECOY_ONLY",
    }
)
"""케이스 유형 7종. **파생할 수 없어서 YAML 에 적는 값이다.**

난이도(EASY/HARD/DECOY/EMPTY)는 다른 필드에서 파생하므로 저장하지 않는다(README §3).
그러나 "왜 EMPTY 인가"(영역 밖 / 신설 / 개정 부분 무관)는 어느 필드에도 없는 의미 판단이고,
§6-3 의 유형별 정확도가 이 축으로 집계된다. **파생 가능한 것은 파생하고, 아닌 것만 적는다.**"""

TYPE_TO_OUTCOME = {
    "IMPACT": "IMPACT",
    "OTHER_LAW_PLAIN": "NO_IMPACT",
    "OTHER_LAW_B1_HIGH": "NO_IMPACT",
    "EMPTY_OUT_OF_SCOPE": "INSUFFICIENT_EVIDENCE",
    "EMPTY_NEW_PROVISION": "INSUFFICIENT_EVIDENCE",
    "EMPTY_PART_UNRELATED": "INSUFFICIENT_EVIDENCE",
    "DECOY_ONLY": "INSUFFICIENT_EVIDENCE",
}
"""유형과 정답의 대응. 둘은 독립된 값이 아니므로 어긋나면 둘 중 하나가 틀린 것이다."""

DECLARED_TYPE_COUNTS = {
    "OTHER_LAW_PLAIN": 7,
    "OTHER_LAW_B1_HIGH": 4,
    "EMPTY_OUT_OF_SCOPE": 5,
    "EMPTY_NEW_PROVISION": 2,
    "EMPTY_PART_UNRELATED": 3,
    "DECOY_ONLY": 1,
    "IMPACT": 20,
}
"""유형별 선언 건수(기존 + 신규). 합이 `DECLARED_TOTAL_CASES` 와 같아야 한다.

기존/신규 내역은 `evals/datasets/golden/README.md` §3 의 표에 있다. **두 축을 섞어
적지 않는다** — 섞은 표를 손으로 검산하려다 43 ≠ 42 가 났다."""

VALID_B1_MATCH_KINDS = frozenset({"OTHER_LAW_CITATION", "INTERNAL_REFERENCE", "PHRASE_OVERLAP"})
"""B-1(조 번호 문자열 매칭) 이 걸린 성격 3종. 근거: `docs/16-baseline-comparison.md`.

- `OTHER_LAW_CITATION` — 사내 문서가 **개정된 그 법령의 그 조문**을 인용해서 걸렸다.
  B-1 이 정답을 맞히는 유일한 경로다
- `INTERNAL_REFERENCE` — 조 번호는 있는데 **개정 법령을 가리키지 않아서** 걸렸다
- `PHRASE_OVERLAP` — 조 번호가 아니라 표현이 겹쳐서 걸렸다

**`INTERNAL_REFERENCE` 의 정의를 2026-08-25 에 넓혔다.** 처음에는 "사내 문서의 **자기
조 번호**와 우연히 같아서"라고 적었는데, 확장분 B-1 실측이 그것을 반증했다. B-1 은 조
번호를 **문단 본문에서** 찾으므로, 걸리는 것은 그 번호를 가진 조가 아니라 그 번호를
**인용한** 문단이다. 실측된 오탐은 두 갈래였다.

  (a) 사내 문서가 자기 문서(또는 다른 사내 문서)의 조를 번호로 인용한 경우 —
      ISP-GUIDE-002#25 의 "제34조에 따른 변경관리 절차"
  (b) 사내 문서가 **다른 법령**의 같은 번호 조문을 인용한 경우 —
      ISP-PROC-002#11 의 "「개인정보 보호법」 제34조제1항" (개정된 것은 정보통신망법
      시행령 제34조다)

둘 다 "조 번호는 맞는데 가리키는 대상이 다르다"이므로 한 값으로 묶는다. (b)를
`OTHER_LAW_CITATION` 으로 세면 **B-1 이 정답을 맞힌 경우와 구별되지 않는다** —
그 구별이 이 필드의 존재 이유이므로 묶는 쪽을 택했다."""

B1_PROBED_FALSE_CONDITION = (
    "`source.article_path` 에서 조 번호를 하나도 읽어내지 못한 경우 (토큰 0개)"
)
"""`b1_probed: false` 가 발화하는 **유일한** 조건.

케이스 단위 B-1 은 `source.article_path` 와 사내 코퍼스 152조만 쓴다 —
`evals/runners/b1_cases.py`. **스냅샷이 필요 없다.** 스냅샷이 필요한 것은 개정 조문
전수를 훑는 `b1_precheck.py` 쪽이고, 둘을 혼동해 "스냅샷이 없는 원천(280277)은
훑지 못한다"고 적었던 것을 2026-08-24 에 정정했다. **42건 전부 훑인다.**

그래도 필드와 검사를 남기는 이유: `article_tokens` 는 `제25조의2 외` 같은 표기에서
조 번호를 못 읽으면 빈 목록을 돌려준다(`evals/runners/baseline.py`). 그때 걸린 것이
0건인 것은 **"훑었는데 없음"이 아니라 "훑을 것이 없음"**이며, 둘을 같은 0 으로 세면
B-1 베이스라인이 실제보다 약해 보인다. 지금 발화하는 케이스는 없다."""

MIN_CASES = DECLARED_TOTAL_CASES
MAX_CASES = DECLARED_TOTAL_CASES


def load(path: Path) -> dict[str, Any]:
    """케이스 하나를 읽는다. `Any` 는 YAML 경계라 좁힐 수 없다."""
    parsed: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name}: 최상위가 매핑이 아니다"
    return parsed


CASES: list[tuple[str, dict[str, Any]]] = [(p.name, load(p)) for p in CASE_PATHS]


def impacts_of(case: dict[str, Any]) -> list[dict[str, Any]]:
    """`expected_impacts` 를 항상 리스트로 돌려준다. None 과 빈 리스트를 같게 다룬다."""
    value = case.get("expected_impacts")
    return list(value) if value else []


def decoys_of(case: dict[str, Any]) -> list[dict[str, Any]]:
    """`decoys` 를 항상 리스트로 돌려준다."""
    value = case.get("decoys")
    return list(value) if value else []


def is_new(case: dict[str, Any]) -> bool:
    """확장분(신규)인가. 원천 상한이 신규에만 걸리므로 이 구별이 필요하다."""
    return str(case["id"]) not in EXISTING_IDS


def test_case_count_matches_declaration() -> None:
    """[검산 1/6] 총 건수가 선언과 일치한다.

    이 테스트가 존재하는 이유: 총계를 손으로 세다 43 ≠ 42 가 났다. 케이스를 하나
    추가·삭제하면 여기서 즉시 드러난다.
    """
    assert len(CASES) == DECLARED_TOTAL_CASES, (
        f"선언 {DECLARED_TOTAL_CASES}건인데 파일은 {len(CASES)}건이다"
    )


def test_ids_are_unique_and_match_filename() -> None:
    """`id` 가 파일명과 일치하고 중복이 없다. 어긋나면 결과 집계가 다른 케이스에 붙는다."""
    ids = [case["id"] for _, case in CASES]
    assert len(ids) == len(set(ids)), "id 중복"
    for name, case in CASES:
        assert case["id"] == name.removesuffix(".yaml"), f"{name}: id 불일치"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_top_level_schema(name: str, case: dict[str, Any]) -> None:
    """최상위 필수 필드와 열거값을 검사한다."""
    for field in ("id", "title", "source", "expected_outcome", "expected_risk", "notes"):
        assert field in case, f"{name}: `{field}` 없음"

    assert case["expected_outcome"] in VALID_OUTCOMES, f"{name}: expected_outcome"
    assert case["expected_risk"] in VALID_RISKS, f"{name}: expected_risk"
    assert isinstance(case.get("expected_departments"), list), f"{name}: expected_departments"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_source_schema(name: str, case: dict[str, Any]) -> None:
    """`source` 의 필수 필드와 열거값을 검사한다."""
    source = case["source"]
    for field in SOURCE_REQUIRED:
        assert field in source, f"{name}: source.{field} 없음"

    assert source["revision_kind"] in VALID_REVISION_KINDS, f"{name}: revision_kind"
    assert source["change_type"] in {c.value for c in ChangeType}, f"{name}: change_type"
    assert len(str(source["law_id"])) == 6, f"{name}: law_id 는 6자리 문자열이어야 한다"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_added_articles_have_no_before(name: str, case: dict[str, Any]) -> None:
    """본조신설(ADDED)은 `before` 가 null 이어야 한다.

    신설 조문에 개정 전 텍스트가 있으면 그것은 다른 조문이거나 지어낸 것이다.
    """
    source = case["source"]
    if source["change_type"] == ChangeType.ADDED.value:
        assert source["before"] is None, f"{name}: ADDED 인데 before 가 있다"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_unverified_before_is_marked(name: str, case: dict[str, Any]) -> None:
    """확보하지 못한 `before` 는 `TODO(verify)` 로 표시하고 사유를 남긴다.

    CLAUDE.md §5.1 — 자리를 비워두는 것은 허용되지만 지어내는 것은 허용되지 않는다.
    빈 문자열이나 "미확인" 같은 자유 문구로 두면 검색으로 찾을 수 없다.
    """
    source = case["source"]
    before = source["before"]
    if isinstance(before, str) and "TODO(verify)" in before:
        assert "unverified" in source["evidence"], (
            f"{name}: before 가 TODO(verify) 인데 evidence.unverified 가 없다. "
            "왜 미확인인지 남기지 않으면 나중에 확인 경로를 알 수 없다"
        )
    else:
        assert before is None or (isinstance(before, str) and before.strip()), (
            f"{name}: before 가 빈 문자열이다. null(신설) 이거나 원문이거나 TODO(verify) 여야 한다"
        )


@pytest.mark.parametrize(("name", "case"), CASES)
def test_impact_entries(name: str, case: dict[str, Any]) -> None:
    """`expected_impacts` 각 항목의 필드와 난이도 값을 검사한다."""
    for impact in impacts_of(case):
        for field in ("doc_id", "article_spec", "difficulty", "content_spec", "why"):
            assert field in impact, f"{name}: expected_impacts.{field} 없음"
        assert impact["doc_id"] in KNOWN_DOC_IDS, f"{name}: 알 수 없는 doc_id {impact['doc_id']}"
        assert impact["difficulty"] in VALID_DIFFICULTIES, f"{name}: difficulty"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_decoy_entries(name: str, case: dict[str, Any]) -> None:
    """decoy 는 `why_not` 을 반드시 갖는다. 없으면 나중에 판정이 흔들린다."""
    for decoy in decoys_of(case):
        for field in ("doc_id", "article_spec", "content_spec", "why_not"):
            assert field in decoy, f"{name}: decoys.{field} 없음"
        assert decoy["doc_id"] in KNOWN_DOC_IDS, f"{name}: 알 수 없는 doc_id {decoy['doc_id']}"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_outcome_matches_impacts(name: str, case: dict[str, Any]) -> None:
    """정답 유형과 `expected_impacts` 의 존재가 어긋나지 않는다.

    IMPACT 인데 대응 문단이 없거나, INSUFFICIENT_EVIDENCE 인데 대응 문단이 있으면
    러너가 무엇을 정답으로 채점해야 할지 알 수 없다.
    """
    outcome = case["expected_outcome"]
    impacts = impacts_of(case)

    if outcome == "IMPACT":
        assert impacts, f"{name}: IMPACT 인데 expected_impacts 가 비어 있다"
    else:
        assert not impacts, f"{name}: {outcome} 인데 expected_impacts 가 있다"

    if outcome == "INSUFFICIENT_EVIDENCE":
        assert case.get("suggested_action", "").strip(), (
            f"{name}: INSUFFICIENT_EVIDENCE 인데 suggested_action 이 없다. "
            "'모른다'로 끝내면 담당자가 다음에 무엇을 할지 알 수 없다"
        )


@pytest.mark.parametrize(("name", "case"), CASES)
def test_no_impact_cases_still_carry_decoys(name: str, case: dict[str, Any]) -> None:
    """정답이 '없음'인 케이스는 반드시 함정을 갖는다.

    대응 문단이 없다는 것만으로는 정밀도를 시험할 수 없다. 끌려올 만한 문단이
    실제로 존재해야 "찾지 않는 것"이 능력으로 측정된다.
    """
    if case["expected_outcome"] in {"NO_IMPACT", "INSUFFICIENT_EVIDENCE"}:
        assert decoys_of(case), f"{name}: 정답이 없음인데 decoy 가 없다"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_article_specs_within_document_size(name: str, case: dict[str, Any]) -> None:
    """참조한 조 번호가 그 문서의 조문 수 안에 있다.

    `article_spec` 은 "제18조 (접속기록의 보관)" 형태다. 앞의 조 번호만 뽑아 비교한다.
    가지번호(제5조의2)는 사내 문서에 쓰지 않기로 했으므로 다루지 않는다.
    """
    for entry in impacts_of(case) + decoys_of(case):
        spec = str(entry["article_spec"])
        match = re.match(r"제(\d+)조", spec)
        assert match is not None, f"{name}: article_spec 형식이 아니다: {spec!r}"

        number = int(match.group(1))
        limit = DOC_ARTICLE_COUNTS[entry["doc_id"]]
        assert 1 <= number <= limit, (
            f"{name}: {entry['doc_id']} 는 {limit}조인데 {spec} 를 가리킨다"
        )


def test_difficulty_distribution() -> None:
    """난이도 분포가 요구를 충족한다 (README §3의 파생 규칙)."""
    easy = sum(1 for _, c in CASES for i in impacts_of(c) if i["difficulty"] == "EASY")
    medium = sum(1 for _, c in CASES for i in impacts_of(c) if i["difficulty"] == "MEDIUM")
    hard = sum(1 for _, c in CASES if len({i["doc_id"] for i in impacts_of(c)}) >= 2)
    with_decoys = sum(1 for _, c in CASES if decoys_of(c))
    empty = sum(1 for _, c in CASES if c["expected_outcome"] == "INSUFFICIENT_EVIDENCE")

    assert easy >= MIN_EASY_IMPACTS, f"EASY {easy}건"
    assert medium >= MIN_MEDIUM_IMPACTS, f"MEDIUM {medium}건"
    assert hard >= MIN_HARD_CASES, f"HARD(2개 이상 문서) {hard}건"
    assert with_decoys >= MIN_DECOY_CASES, f"decoy 있는 케이스 {with_decoys}건"
    assert MIN_EMPTY_CASES <= empty <= MAX_EMPTY_CASES, f"EMPTY {empty}건"


EMPTY_TYPES = frozenset(
    {"EMPTY_OUT_OF_SCOPE", "EMPTY_NEW_PROVISION", "EMPTY_PART_UNRELATED", "DECOY_ONLY"}
)
"""정답이 `INSUFFICIENT_EVIDENCE` 인 유형 4종. `TYPE_TO_OUTCOME` 에서 파생할 수 있으나
**여기서 다시 적는다** — 파생하면 `TYPE_TO_OUTCOME` 이 틀렸을 때 이 검사도 함께 틀린다."""

MIN_EMPTY_SOURCE_LAWS = 4
"""EMPTY 케이스가 나온 서로 다른 법령의 최소 수.

**15건 시절에는 「EMPTY 는 전부 다른 법령에서 나온다」였다.** 그 규칙은 EMPTY 가 3건일 때
성립했고 11건에서는 성립할 수 없다 — 12개월 전수에서 우리 대상 법령이 9종이기 때문이다
(`config/corpus.yaml`). 규칙을 그대로 두면 확장이 불가능해지므로 **대리 지표를 버리고
직접 지표로 바꿨다.**

「서로 다른 이유로 EMPTY 인가」는 이제 `case_type` 이 직접 말한다(4종). 법령 다양성은
그와 별개로 남겨 두되 하한만 건다 — 한 법령에 몰리면 "그 법령은 무관하다" 하나만
시험하게 되는 원래의 위험은 그대로이기 때문이다."""


VALID_COVERAGE_REASONS = frozenset(
    {"MULTI_ARTICLE", "NO_SNAPSHOT", "ARTICLE_NOT_FOUND", "EMPTY_ASSEMBLY"}
)
"""`after_coverage.ratio` 가 null 인 사유 4종. `scripts/analysis/after_coverage.py` 가 낸다.

**0.0 으로 채우지 않는 이유**가 이 값들이다 — "덮임이 0이다"와 "잴 수 없다"는 다른 사실이고,
후자는 다시 넷으로 갈린다. `MULTI_ARTICLE` 은 케이스 설계상 정상이고 `ARTICLE_NOT_FOUND` 는
결함이다."""

MIN_AFTER_COVERAGE = 0.04
"""`source.after` 가 조문 전문(`assemble_body()`)의 몇 %여야 하는가의 하한.

**이 상수는 한 번 무너졌다. 그 경위가 값보다 중요하다.**

처음에는 유형별로 둘로 나눴다 — IMPACT 0.10 / 영향 없음 0.05. 근거는 추론이었다:
"IMPACT 는 정답 문단을 찾아야 하므로 입력이 얇으면 채점이 다른 것을 잰다."
그 추론에 따라 case-023(덮임 0.0421)을 조문 전문으로 교체하고 42건을 돌렸다.

**실측이 그 추론을 뒤집었다.** 전문으로 바꾸자 정답 2개 중 1개가 **검색 밖으로
밀려났다** — 6,943자 중 개정된 두 호가 292자이고 나머지 정의 조항이 질의를 끌고 갔다.
검색은 결정론적이므로(`docs/15-variability-results.md`) 입력 변경이 원인이다.
그래서 발췌로 되돌렸고 덮임은 다시 0.0421 이 됐다.

**남은 것은 하한 하나뿐이며 뜻이 달라졌다.** 이제 이 값은 "얇으면 나쁘다"를 주장하지
않는다 — 우리가 가진 유일한 측정은 그 반대를 말한다. 이 값이 말하는 것은
**"지금 받아들인 것보다 더 얇아지면 사람이 한 번 본다"** 뿐이다.

| | 관측 최솟값 (2026-08-25, 34건) |
|---|---|
| IMPACT | 0.0421 (case-023) |
| 영향 없음 | 0.0569 (case-038) |

0.04 는 그 아래 첫 자리다. **유형별로 나누지 않는다** — 나눌 근거였던 추론이 기각됐고,
근거 없는 상수를 만들지 않는다(CLAUDE.md §4).

**이 하한은 case-023 을 잡은 적이 없다.** 0.0421 과 다음 값 0.0569 의 간격이 0.015 이고
둘을 가르는 자연스러운 절단이 없다. 023 을 두 번(전문으로, 다시 발췌로) 움직인 것은
하한이 아니라 사람 판단이며 그 판단의 근거는 두 조건의 실측이다.
**하한이 무엇을 못 잡는지를 적어 두지 않으면 다음 사람이 이 검사를 신뢰한다.**"""


@pytest.mark.parametrize(("name", "case"), CASES)
def test_after_coverage_is_recorded(name: str, case: dict[str, Any]) -> None:
    """[검산 7/7] `source.after` 의 조문 전문 대비 덮임이 기록돼 있고 하한 위에 있다.

    이 테스트가 존재하는 이유: **`source.after` 는 조문 전문이 아니라 개정된 항이다**
    (README §6.1). 그 관례가 암묵적이었던 동안 case-023 이 조문 전문의 **4.2%** 로
    들어갔고, 그 케이스는 다른 41건과 다른 입력으로 채점되고 있었다. 관례를 문서에 적고
    하한을 여기서 강제한다.

    측정은 스냅샷을 읽어야 하는데 `data/snapshots/` 는 커밋하지 않으므로, 이 테스트는
    **기록된 값**을 검사한다. 재측정과 표류 확인은 `scripts/analysis/after_coverage.py`
    가 한다.
    """
    coverage = case.get("after_coverage")
    assert isinstance(coverage, dict), (
        f"{name}: after_coverage 가 없다. "
        "`uv run python scripts/analysis/after_coverage.py --write` 로 채운다"
    )

    ratio = coverage.get("ratio")
    if ratio is None:
        assert coverage.get("reason") in VALID_COVERAGE_REASONS, (
            f"{name}: ratio 가 null 인데 사유가 {coverage.get('reason')!r} 이다"
        )
        assert coverage.get("reason") != "ARTICLE_NOT_FOUND", (
            f"{name}: article_path 가 스냅샷에 없는 조문을 가리킨다"
        )
        return

    assert ratio >= MIN_AFTER_COVERAGE, (
        f"{name}: after 가 조문 전문의 {ratio:.4f} 로 하한 {MIN_AFTER_COVERAGE} 미만이다. "
        f"({coverage.get('after_chars')}/{coverage.get('assembled_chars')}자) "
        "지금까지 받아들인 것보다 얇다 — 의도한 것인지 사람이 확인한다"
    )


def test_empty_cases_have_distinct_reasons() -> None:
    """EMPTY 케이스가 4종 사유를 모두 덮고, 한 법령에 몰리지 않는다.

    같은 이유의 EMPTY 만 모으면 "모른다고 말하는 능력"의 한 면만 시험하게 된다.
    **사유는 `case_type` 이 직접 말하므로 대리 지표를 쓰지 않는다** — 15건 시절에는
    사유를 적을 필드가 없어 "원천 법령이 다른가"를 대신 봤다.
    """
    empties = [c for _, c in CASES if c["expected_outcome"] == "INSUFFICIENT_EVIDENCE"]
    types = {str(c["case_type"]) for c in empties}
    laws = {c["source"]["law_id"] for c in empties}

    assert types == EMPTY_TYPES, f"EMPTY 사유 4종이 다 있어야 한다. 실제: {sorted(types)}"
    assert len(laws) >= MIN_EMPTY_SOURCE_LAWS, (
        f"EMPTY 케이스가 {len(laws)}개 법령에서만 나왔다 (최소 {MIN_EMPTY_SOURCE_LAWS}): "
        f"{sorted(laws)}"
    )


def test_cross_law_promulgation_group_is_present() -> None:
    """공포번호 21445가 세 법령을 건드린 (가) 시나리오가 살아 있다.

    이 묶음은 12개월 전수에서 우연히 나온 것이라 잃으면 다시 만들 수 없다.
    한 공포 안에서 실질개정과 타법개정을 갈라내는 유일한 시험이다.
    """
    group = [c for _, c in CASES if c["source"]["promulgation_no"] == "21445"]
    laws = {c["source"]["law_id"] for c in group}
    kinds = {c["source"]["revision_kind"] for c in group}

    assert len(laws) == 3, f"공포번호 21445 케이스의 법령이 3종이 아니다: {laws}"
    assert kinds == {"일부개정", "타법개정"}, f"실질개정과 타법개정이 모두 있어야 한다: {kinds}"


def test_type_counts_sum_to_total() -> None:
    """[검산 2/6] 유형별 합이 총 건수와 같고, 유형별 실제 건수가 선언과 일치한다.

    두 가지를 함께 본다. 합만 보면 두 유형이 서로 상쇄된 오류를 놓치고, 유형별만 보면
    선언 표 자체가 총계와 어긋난 것을 놓친다. **43 ≠ 42 가 정확히 후자였다.**
    """
    assert sum(DECLARED_TYPE_COUNTS.values()) == DECLARED_TOTAL_CASES, (
        f"선언 표의 합 {sum(DECLARED_TYPE_COUNTS.values())} ≠ 총계 {DECLARED_TOTAL_CASES}"
    )

    actual = Counter(str(case["case_type"]) for _, case in CASES)
    assert dict(actual) == DECLARED_TYPE_COUNTS, (
        f"유형별 실제 {dict(sorted(actual.items()))} ≠ 선언 {DECLARED_TYPE_COUNTS}"
    )


def test_no_impact_ratio_meets_target() -> None:
    """[검산 3/6] 「영향 없음」이 정답인 비율이 목표(50% 이상)를 충족한다.

    이 비율이 이 확장의 이유다. 15건 시절 골든셋은 IMPACT 10 / 영향 없음 5 로 실제
    운영 분포(타법개정만 56.2%)보다 쉬웠고, 여기서 잰 정밀도는 낙관적이었다.
    """
    no_impact = sum(1 for _, c in CASES if c["expected_outcome"] != "IMPACT")
    impact = sum(1 for _, c in CASES if c["expected_outcome"] == "IMPACT")

    assert no_impact == DECLARED_NO_IMPACT_CASES, f"영향 없음 {no_impact}건"
    assert impact == DECLARED_IMPACT_CASES, f"IMPACT {impact}건"
    assert no_impact / len(CASES) >= MIN_NO_IMPACT_RATIO, (
        f"영향 없음 비율 {no_impact / len(CASES):.4f} < {MIN_NO_IMPACT_RATIO}"
    )


def test_new_cases_respect_per_source_cap() -> None:
    """[검산 4/6] 신규 케이스가 원천당 상한을 지킨다 (IMPACT 3 / 영향 없음 4).

    한 원천에 몰리면 그 원천 하나의 성질이 유형별 정확도를 대표해 버린다. 상한을
    **신규에만** 거는 이유는 `MAX_NEW_IMPACT_PER_SOURCE` 의 docstring 에 있다.
    """
    impact_per_source = Counter(
        str(c["source"]["mst"]) for _, c in CASES if is_new(c) and c["expected_outcome"] == "IMPACT"
    )
    other_per_source = Counter(
        str(c["source"]["mst"]) for _, c in CASES if is_new(c) and c["expected_outcome"] != "IMPACT"
    )

    over_impact = {m: n for m, n in impact_per_source.items() if n > MAX_NEW_IMPACT_PER_SOURCE}
    over_other = {m: n for m, n in other_per_source.items() if n > MAX_NEW_NO_IMPACT_PER_SOURCE}

    assert not over_impact, (
        f"신규 IMPACT 가 원천당 {MAX_NEW_IMPACT_PER_SOURCE}건을 넘는다: {over_impact}"
    )
    assert not over_other, (
        f"신규 영향 없음이 원천당 {MAX_NEW_NO_IMPACT_PER_SOURCE}건을 넘는다: {over_other}"
    )


def test_source_count_matches_declaration() -> None:
    """[검산 5/6] 서로 다른 원천의 수가 선언과 일치한다.

    나열된 MST 16개와 "원천 15개"가 어긋난 것이 §7 의 두 번째 발견이었다. 원인은
    기존 케이스의 원천을 신규로 세었기 때문이고, **그것은 목록을 눈으로 훑어서는
    드러나지 않는다.** 여기서 센다.

    `source_id` 를 함께 검사하는 이유: 그 필드가 `source.mst` 와 어긋나면 원천별
    집계가 두 개의 다른 답을 낸다.
    """
    for name, case in CASES:
        assert case["source_id"] == f"MST-{case['source']['mst']}", (
            f"{name}: source_id 가 source.mst 와 어긋난다"
        )

    sources = {str(c["source"]["mst"]) for _, c in CASES}
    assert len(sources) == DECLARED_SOURCE_COUNT, (
        f"원천 {len(sources)}개 (선언 {DECLARED_SOURCE_COUNT}개): {sorted(sources)}"
    )


@pytest.mark.parametrize(("name", "case"), CASES)
def test_b1_fields_are_consistent(name: str, case: dict[str, Any]) -> None:
    """[검산 6/6] B-1 필드가 서로 정합한다.

    `b1_matched` 가 false 인데 걸린 조항이 남아 있거나 `b1_match_kind` 가 채워져 있으면,
    §6-3 의 B-1 대조가 조용히 틀린 답을 낸다. **빈 칸의 의미를 구별하는 것이 요점이다** —
    "훑었는데 안 걸렸다"(matched=false)와 "훑지 못했다"(probed=false)는 다른 사실이며,
    후자는 스냅샷이 없어서 생긴다.
    """
    probed = case["b1_probed"]
    matched = case["b1_matched"]
    articles = case["b1_matched_articles"]
    kind = case["b1_match_kind"]

    assert isinstance(probed, bool), f"{name}: b1_probed 는 bool 이어야 한다"
    assert isinstance(articles, list), f"{name}: b1_matched_articles 는 배열이어야 한다"

    if not probed:
        # 발화 조건은 하나뿐이다 — `B1_PROBED_FALSE_CONDITION` 참조.
        assert matched is None, f"{name}: 훑지 못했는데 b1_matched 가 {matched!r} 이다"
        assert not articles and kind is None, f"{name}: 훑지 못했는데 결과가 채워져 있다"
        assert str(case.get("b1_note", "")).strip(), (
            f"{name}: b1_probed=false 인데 왜 못 훑었는지가 없다"
        )
        return

    assert isinstance(matched, bool), f"{name}: 훑었으면 b1_matched 는 bool 이어야 한다"
    if matched:
        assert articles, f"{name}: b1_matched=true 인데 걸린 조항이 비어 있다"
        assert kind in VALID_B1_MATCH_KINDS, f"{name}: b1_match_kind={kind!r}"
    else:
        assert not articles, f"{name}: b1_matched=false 인데 걸린 조항이 있다"
        assert kind is None, f"{name}: b1_matched=false 인데 b1_match_kind 가 있다"


@pytest.mark.parametrize(("name", "case"), CASES)
def test_case_type_matches_outcome(name: str, case: dict[str, Any]) -> None:
    """유형과 정답이 어긋나지 않는다. 어긋나면 유형별 집계가 다른 케이스를 센다."""
    case_type = case["case_type"]
    assert case_type in VALID_CASE_TYPES, f"{name}: 알 수 없는 case_type {case_type!r}"
    assert case["expected_outcome"] == TYPE_TO_OUTCOME[case_type], (
        f"{name}: {case_type} 의 정답은 {TYPE_TO_OUTCOME[case_type]} 여야 하는데 "
        f"{case['expected_outcome']} 이다"
    )


def test_other_law_types_are_actually_other_law() -> None:
    """타법개정 유형은 `revision_kind` 가 실제로 타법개정이어야 한다.

    유형은 사람이 적는 값이고 `revision_kind` 는 법제처가 준 값이다. 어긋나면
    **사람이 적은 쪽이 틀린 것**이며, 그대로 두면 타법개정 정확도가 다른 것을 잰다.
    """
    for name, case in CASES:
        if str(case["case_type"]).startswith("OTHER_LAW"):
            assert case["source"]["revision_kind"] == "타법개정", (
                f"{name}: {case['case_type']} 인데 제개정구분이 "
                f"{case['source']['revision_kind']} 이다"
            )


def test_contrast_pair_on_same_article_path() -> None:
    """같은 조문 경로에 정답이 갈리는 (나) 대조쌍이 살아 있다.

    정보통신망법 제45조의3에 대해 하나는 IMPACT, 하나는 NO_IMPACT 여야 한다.
    조문 경로 일치를 영향 판정 근거로 쓰지 못하게 하는 유일한 케이스다.
    """
    pair = [
        c
        for _, c in CASES
        if c["source"]["law_id"] == "000030" and c["source"]["article_path"].startswith("제45조의3")
    ]
    outcomes = {c["expected_outcome"] for c in pair}

    assert len(pair) == 2, f"제45조의3 대조쌍이 2건이 아니다: {len(pair)}건"
    assert outcomes == {"IMPACT", "NO_IMPACT"}, f"정답이 갈리지 않는다: {outcomes}"
