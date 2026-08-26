"""인용 적합도 판정지가 사람의 판정을 앵커링하지 않는지 고정한다.

이 테스트가 존재하는 이유: 판정지의 값은 **사람이 gate 3단의 판단을 보지 않고 매긴
등급**이다. 기계가 이미 무엇이라 했는지가 한 조각이라도 표에 실리면 그 순간 사람은
그것을 읽고 판정하게 되고, 그것은 이 저장소가 de-anchored 검증기를 만들며 문제 삼았던
바로 그 기전이다 (`docs/12` §12). 규칙이 `render_sheet` 안에만 있어 타입이 강제하지
못하므로 여기서 고정한다.

주장 문장의 형태도 함께 고정한다 — 부서 주장은 `pipeline/impact.py` 가 조립한 문장을
그대로 재현해야 한다. 다른 문장을 보여 주면 사람과 gate 가 다른 것을 판정하게 되고,
두 판정을 대조한 비율이 F-6 의 실측치가 되지 못한다.

채점 쪽도 고정한다: `PARTIAL` 을 `UNSUPPORTED` 에 합치지 않는 방침은 **판정 전에**
정해졌으므로 코드에 박혀 있어야 하고, 빈 판정은 조용히 넘어가면 안 된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.runners.citation_adequacy import (
    LEVELS,
    SHUFFLE_SEED,
    Row,
    claim_of,
    read_verdicts,
    render_sheet,
    score,
    shuffle,
)

DRAFT: dict[str, Any] = {
    "impacts": [
        {"paragraph_id": "p-1", "quote": "인용문 1", "claim": "주장 1"},
        {"paragraph_id": "p-2", "quote": "인용문 2", "claim": "주장 2"},
    ],
    "departments": [
        {
            "department": "정보보호부",
            "basis_paragraph_id": "p-3",
            "basis_quote": "근거 인용문",
            "rationale": "명시된 주체다",
        }
    ],
}


def _row(**overrides: str) -> Row:
    base = {
        "case_id": "case-001",
        "case_type": "IMPACT",
        "expected_outcome": "IMPACT",
        "impact_status": "NEEDS_REVIEW",
        "key": "impact:0",
        "claim": "주장 1",
        "quote": "인용문 1",
        "spec": "ISP-POL-001 제5조(경영진의 책임)",
        "paragraph_id": "p-1",
        "paragraph_text": "문단 전문이다.",
        "gate_level": "SUPPORTED",
        "gate_reason": "판정 사유는 앵커가 된다",
    }
    base.update(overrides)
    return Row(**base)


def test_claim_of_impact() -> None:
    assert claim_of(DRAFT, "impact:1") == ("주장 2", "인용문 2", "p-2")


def test_claim_of_department_reproduces_pipeline_wording() -> None:
    # pipeline/impact.py: f"{entry.department}가 관여한다: {entry.rationale}"
    claim, quote, paragraph_id = claim_of(DRAFT, "dept:0")
    assert claim == "정보보호부가 관여한다: 명시된 주체다"
    assert (quote, paragraph_id) == ("근거 인용문", "p-3")


def test_claim_of_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="알 수 없는 판정 키"):
        claim_of(DRAFT, "risk:0")


def test_sheet_leaks_no_machine_verdict() -> None:
    # 등급·사유·케이스 ID·유형·실행 판정 다섯 가지가 전부 빠져 있어야 한다.
    sheet = render_sheet([_row()], Path("run.json"))
    body = sheet.split("## 항목", maxsplit=1)[1]
    assert "판정 사유는 앵커가 된다" not in sheet
    assert "case-001" not in sheet
    assert "NEEDS_REVIEW" not in sheet
    # 「SUPPORTED」는 판정 기준 설명에만 나오고, 행에는 값으로 박혀 있지 않다.
    assert "case_type" not in sheet
    assert "IMPACT" not in body


def test_sheet_carries_what_the_human_needs() -> None:
    sheet = render_sheet([_row()], Path("run.json"))
    for expected in ("주장 1", "인용문 1", "문단 전문이다.", "ISP-POL-001 제5조"):
        assert expected in sheet


def test_sheet_fixes_the_reading_before_it_is_filled() -> None:
    # 0/n 의 상한을 미리 싣는다 — 결과를 보고 해석을 정하지 않기 위한 장치다.
    sheet = render_sheet([_row()], Path("run.json"))
    assert "Clopper-Pearson" in sheet
    assert "PARTIAL` 은 **`UNSUPPORTED` 에 합치지 않는다" in sheet
    assert str(SHUFFLE_SEED) in sheet


def test_empty_sheet_says_there_was_nothing_to_judge() -> None:
    # 빈 표와 「판정하지 않았다」가 같은 화면이 되면 분모 0 이 성적으로 읽힌다.
    sheet = render_sheet([], Path("run.json"))
    assert "판정할 것이 없다" in sheet


def test_shuffle_is_deterministic_and_actually_reorders() -> None:
    rows = [_row(case_id=f"case-{n:03d}", key=f"impact:{n}") for n in range(1, 13)]
    first = shuffle(rows)
    assert [r.case_id for r in shuffle(rows)] == [r.case_id for r in first]
    assert [r.case_id for r in first] != [r.case_id for r in rows]
    assert sorted(r.case_id for r in first) == sorted(r.case_id for r in rows)


def _sheet_with(levels: list[str], tmp_path: Path) -> Path:
    lines = ["| # | 인용 문단 | **판정** |", "|---|---|---|"]
    lines += [f"| {i} | 문단 | {lvl} |" for i, lvl in enumerate(levels, start=1)]
    path = tmp_path / "sheet.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_read_verdicts_refuses_blanks(tmp_path: Path) -> None:
    sheet = _sheet_with(["SUPPORTED", "", "PARTIAL"], tmp_path)
    with pytest.raises(ValueError, match="판정이 비어 있는 행"):
        read_verdicts(sheet, 3)


def test_read_verdicts_refuses_unknown_level(tmp_path: Path) -> None:
    sheet = _sheet_with(["SUPPORTED", "OK", "PARTIAL"], tmp_path)
    with pytest.raises(ValueError, match="알 수 없는 등급"):
        read_verdicts(sheet, 3)


def test_read_verdicts_refuses_row_count_mismatch(tmp_path: Path) -> None:
    sheet = _sheet_with(["SUPPORTED", "PARTIAL"], tmp_path)
    with pytest.raises(ValueError, match="행이어야 한다"):
        read_verdicts(sheet, 3)


def test_read_verdicts_accepts_backticked_levels(tmp_path: Path) -> None:
    sheet = _sheet_with(["`SUPPORTED`", "`UNSUPPORTED`"], tmp_path)
    assert read_verdicts(sheet, 2) == {1: "SUPPORTED", 2: "UNSUPPORTED"}


def _payload(n: int) -> dict[str, Any]:
    return {
        "rows": [
            {
                "sheet_row": i,
                "case_id": f"case-{i:03d}",
                "key": "impact:0",
                "spec": "문단",
                "gate_reason": "사유",
            }
            for i in range(1, n + 1)
        ]
    }


def test_score_does_not_fold_partial_into_unsupported() -> None:
    # 방침은 판정 전에 고정됐다. PARTIAL 이 F-6 을 키우면 안 된다.
    verdicts = {1: "SUPPORTED", 2: "PARTIAL", 3: "PARTIAL", 4: "UNSUPPORTED"}
    result = score(_payload(4), verdicts)
    assert result["f6"] == 0.25
    assert result["mismatch_rate"] == 0.75
    assert result["agreement_rate"] == 0.25
    assert [d["sheet_row"] for d in result["disagreements"]] == [2, 3, 4]


def test_score_zero_still_reports_an_upper_bound() -> None:
    verdicts = dict.fromkeys(range(1, 21), "SUPPORTED")
    result = score(_payload(20), verdicts)
    assert result["f6"] == 0.0
    assert result["f6_ci95"][1] == pytest.approx(0.1684, abs=1e-3)


def test_score_refuses_empty_denominator() -> None:
    with pytest.raises(ValueError, match="0건이면"):
        score({"rows": []}, {})


def test_levels_match_the_gate_scale() -> None:
    # 다른 어휘를 쓰면 사람 판정과 gate 판정을 나란히 놓을 수 없다.
    assert LEVELS == ("SUPPORTED", "PARTIAL", "UNSUPPORTED")
