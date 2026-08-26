"""반복 실행 결과를 대조해 **변동성의 크기**를 잰다 (6단계 §1).

    uv run python -m evals.runners.variability --kind retrieval --files a.json b.json c.json
    uv run python -m evals.runners.variability --kind impact --files a.json b.json c.json

무엇을 재는가 (`docs/14-variability-protocol.md` §3):

1. **집계 지표의 회차 간 최대-최소** — 합계가 얼마나 흔들리는가
2. **케이스 단위 불안정 건수** — 집계는 상쇄되지만 케이스는 상쇄되지 않는다

**이 러너는 측정하지 않는다. 이미 나온 결과 파일만 읽는다.** 측정과 집계를 나눈 이유는
집계 코드를 고치는 것이 측정을 다시 돌리는 것과 같은 일이 되지 않게 하기 위해서다 —
같은 원본에서 다시 집계할 수 있어야 "집계가 틀렸다"와 "측정이 흔들렸다"를 가른다.

**판정 기준은 여기 없다.** 기준은 `docs/14-variability-protocol.md` §4 에 측정 전에
고정돼 있고, 이 러너는 그 기준에 넣을 수치만 낸다. 기준을 코드에 두면 결과를 보고
슬쩍 고칠 수 있다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

IMPACT_SUMMARY_KEYS = (
    "impact_with_evidence",
    "impact_hit_any",
    "empty_correct",
    "dept_exact",
    "dept_any",
    "dept_recall",
    "unsupported_total",
    "decoy_cited",
    "risk_exact",
    "estimated_cost_usd",
)
"""회차 간 편차를 볼 집계 지표. 규약 §3.1 의 표와 같은 항목이다."""

CASE_JUDGMENT_KEYS = ("impact_status", "hit", "departments")
"""케이스 「불안정」 판정에 쓰는 세 값 (규약 §3.2).

`cited` 를 넣지 않는다 — decoy 를 하나 더 인용했다 빼는 것은 성능 변동이지 판정
변동이 아니다. 참고값으로 따로 센다."""


def _load(paths: list[Path]) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def _spread(values: list[float | None]) -> dict[str, Any]:
    """최대-최소. `None` 이 섞이면 그 사실을 그대로 남긴다."""
    present = [v for v in values if v is not None]
    if not present:
        return {"values": values, "spread": None}
    return {
        "values": values,
        "min": min(present),
        "max": max(present),
        "spread": round(max(present) - min(present), 4),
    }


def invalid_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """비교에 쓸 수 없는 실행을 골라낸다 (2026-08-22 사건 이후 추가).

    호출이 한 번이라도 실패한 실행은 **다른 실행과 비교할 수 없다.** 실패한 케이스가
    채점에서 빠지면 남은 케이스만으로 계산된 비율이 나오고, 그 비율은 정상 실행의
    비율과 같은 이름을 갖는다 — 이름이 같으면 비교된다.

    옛 결과 파일에는 `valid` 키가 없다. 그 경우 **토큰 0** 을 무효 신호로 쓴다 —
    성공한 호출은 반드시 토큰을 쓴다.

    **이 대체 신호는 전량 실패만 잡는다.** 중간에 실패한 옛 실행(토큰 > 0)은 걸러내지
    못하며, 그것이 2026-08-22 회차 1 이다. 옛 파일을 완전히 판정할 방법은 없고, 그래서
    `valid` 키를 만들었다. 옛 파일은 사람이 판정해 문서에 적는다.
    """
    bad = []
    for index, run in enumerate(runs):
        summary = run.get("summary", {})
        reason = None
        if summary.get("valid") is False:
            errors = len(summary.get("call_errors") or [])
            reason = f"호출 실패 {errors}건, 중단={summary.get('aborted')}"
        elif "valid" not in summary and not summary.get("input_tokens"):
            reason = "토큰 0 — 호출이 한 번도 성공하지 않았다 (valid 키 이전 파일)"
        if reason:
            bad.append({"index": index, "reason": reason})
    return bad


def compare_impact(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """LLM 경로 반복 결과를 대조한다 (**무효 실행이 섞이면 집계하지 않는다**)."""
    bad = invalid_runs(runs)
    if bad:
        return {
            "runs": len(runs),
            "aggregated": False,
            "invalid_runs": bad,
            "note": (
                "무효 실행이 섞여 있어 집계하지 않는다. 편차를 말하려면 정상 실행만 "
                "모아야 한다 — 실패한 실행의 수치는 편차가 아니라 결측이다"
            ),
        }
    aggregate = {
        key: _spread([run["summary"].get(key) for run in runs]) for key in IMPACT_SUMMARY_KEYS
    }
    for level in ("SUPPORTED", "PARTIAL", "UNSUPPORTED"):
        aggregate[f"grounding:{level}"] = _spread(
            [run["summary"].get("grounding", {}).get(level, 0) for run in runs]
        )
    aggregate["transferred"] = _spread(
        [
            sum(1 for c in run["cases"] if c["impact_status"] == "INSUFFICIENT_EVIDENCE")
            for run in runs
        ]
    )

    by_case: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        for row in run["cases"]:
            by_case.setdefault(row["case_id"], []).append(row)

    unstable: list[dict[str, Any]] = []
    cited_only: list[str] = []
    for case_id, rows in sorted(by_case.items()):
        if len(rows) < len(runs):
            unstable.append({"case_id": case_id, "reason": "MISSING_RUN", "runs": len(rows)})
            continue
        differing = {
            key
            for key in CASE_JUDGMENT_KEYS
            if len({json.dumps(r[key], ensure_ascii=False, sort_keys=True) for r in rows}) > 1
        }
        if differing:
            unstable.append(
                {
                    "case_id": case_id,
                    "differing": sorted(differing),
                    "values": {key: [r[key] for r in rows] for key in sorted(differing)},
                }
            )
        elif len({json.dumps(sorted(r["cited"]), ensure_ascii=False) for r in rows}) > 1:
            cited_only.append(case_id)

    return {
        "runs": len(runs),
        "cases": len(by_case),
        "aggregate": aggregate,
        "unstable_cases": unstable,
        "unstable_count": len(unstable),
        "cited_only_variation": cited_only,
    }


def compare_retrieval(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """검색 경로 반복 결과를 대조한다. **완전 일치가 기대값이다** (샘플링이 없다)."""
    modes = sorted(set().union(*(set(run["modes"]) for run in runs)))
    out: dict[str, Any] = {"runs": len(runs), "modes": {}}
    for mode in modes:
        summaries = [run["modes"][mode]["summary"] for run in runs]
        keys = sorted(set().union(*(set(s) for s in summaries)))
        numeric = {
            key: _spread([s.get(key) for s in summaries])
            for key in keys
            if all(isinstance(s.get(key), int | float) for s in summaries)
        }
        by_case: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            for row in run["modes"][mode]["cases"]:
                by_case.setdefault(row["case_id"], []).append(row)
        differing = sorted(
            case_id
            for case_id, rows in by_case.items()
            if len({json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows}) > 1
        )
        out["modes"][mode] = {
            "summary_spread": numeric,
            "cases_differing": differing,
            "identical": not differing,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="반복 실행 대조 (6단계 §1)")
    parser.add_argument("--kind", choices=["impact", "retrieval"], required=True)
    parser.add_argument("--files", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    runs = _load(args.files)
    report = compare_impact(runs) if args.kind == "impact" else compare_retrieval(runs)
    report["sources"] = [str(p.resolve().relative_to(REPO_ROOT)) for p in args.files]

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)  # noqa: T201 — 이 러너의 결과물은 사람이 읽는 표준출력이다


if __name__ == "__main__":
    main()
