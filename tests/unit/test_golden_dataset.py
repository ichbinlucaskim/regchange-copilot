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
MAX_EMPTY_CASES = 3
"""`evals/datasets/golden/README.md` §3의 요구. 상한이 있는 것은 EMPTY 뿐이다 —
EMPTY 가 너무 많으면 "항상 모른다"고 답하는 시스템이 높은 점수를 받는다."""

MIN_CASES = 10
MAX_CASES = 15


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


def test_case_count_within_range() -> None:
    """케이스 수가 요구 범위(10~15)에 있다."""
    assert MIN_CASES <= len(CASES) <= MAX_CASES, f"케이스 {len(CASES)}건"


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


def test_empty_cases_have_distinct_reasons() -> None:
    """EMPTY 케이스는 서로 다른 법령에서 나온다.

    같은 법령에서 EMPTY 를 여러 개 뽑으면 "그 법령은 무관하다"는 한 가지만 시험하게
    된다. 서로 다른 이유로 EMPTY 인지는 `notes` 로 관리하고, 여기서는 기계적으로
    확인 가능한 대리 지표(원천 법령이 다른가)만 검사한다.
    """
    empties = [c for _, c in CASES if c["expected_outcome"] == "INSUFFICIENT_EVIDENCE"]
    laws = [c["source"]["law_id"] for c in empties]
    assert len(laws) == len(set(laws)), f"EMPTY 케이스가 같은 법령에서 나왔다: {laws}"


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
