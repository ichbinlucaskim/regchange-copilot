"""42건 영향평가 결과를 집계한다 — 모델을 다시 부르지 않고 기록된 것만 읽는다.

    uv run --group eval python -m evals.runners.impact_report
    uv run --group eval python -m evals.runners.impact_report --result <경로>

목적:
    `impact_eval` 이 남긴 결과 파일과 `llm_invocation` 의 `retrieved_chunk_ids` 만 읽어
    §4 지시가 요구한 집계 7종을 낸다. 핵심은 **(a) 검색이 못 찾았다 / (b) 찾았는데
    안 골랐다** 의 비율이며, 그 둘을 가르는 데 필요한 것이 검색 결과 집합이다.

구현 이유:
    **진단에 LLM 을 다시 부르지 않는다.** 같은 케이스를 다시 돌리면 그때의 검색 결과가
    아니라 지금의 검색 결과를 보게 되고, 변동성 실측(R-27)이 케이스 단위로 15건 중
    10건이 흔들린다고 말한 이상 그 둘은 같은 것이 아니다. `llm_invocation` 은
    **첫 호출부터** 검색 결과 ID 를 남기므로(ADR-013) 그 기록이 그때의 사실이다.

    결과 파일과 DB 를 함께 읽는다. 결과 파일에는 `cited`/`expected` 가 있고 검색 결과
    집합은 없다 — 둘을 합쳐야 (a)/(b) 가 갈린다.

트레이드오프:
    `retrieved_chunk_ids` 는 문단 UUID 이고 케이스의 정답은 `DOC#조번호` 다. 매핑에
    `policy_paragraph` 를 조회하므로 **DB 가 없으면 (a)/(b) 를 낼 수 없다.** 그때는
    `null` 과 사유를 내고 0 으로 채우지 않는다 — 빈 칸 5종의 「측정 안 함」이다.

    유형별·원천별 집계는 결과 파일만으로 되므로 DB 없이도 나온다. 둘을 한 스크립트에
    둔 대신 실패 지점을 분리했다.

엣지 케이스:
    - 결과 파일에 `by_type` 이 없음(옛 실행): 그 칸을 `null` 로 낸다. 옛 파일을 새
      기준으로 다시 세면 두 실행이 같은 것을 잰 것처럼 보인다
    - `impact_assessment_id` 가 없는 호출 행(추출 단계): (a)/(b) 대조에서 제외한다.
      추출 단계의 검색 결과는 영향평가가 본 것과 같지만, 같다는 것을 여기서 가정하지
      않고 영향평가 행만 쓴다
    - 정답이 없는 케이스(영향 없음 22건): (a)/(b) 는 **케이스에 해당 없음**이다.
      분모에 넣으면 "찾을 것이 없어서 안 찾았다"가 정답률로 섞인다
    - 문단 UUID 가 `policy_paragraph` 에 없음: `UNKNOWN_CHUNK` 로 세고 조용히 버리지
      않는다. 코퍼스를 다시 적재하면 UUID 가 바뀌므로 그 사실이 보여야 한다
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import yaml

from regchange.config.settings import apply_dotenv
from regchange.store.dsn import DbRole, role_dsn

logger = logging.getLogger("impact_report")

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"

CONCENTRATED_SOURCES = ("285199", "283839")
"""IMPACT 가 몰린 두 원천. 20건 중 15건이 여기서 나온다.

**이 둘을 뺀 정확도를 함께 내기 위한 상수다.** 편중이 결과를 얼마나 끌었는지는
전체 정확도만 봐서는 보이지 않는다 (`evals/datasets/golden/README.md` §3.4)."""

EXISTING_IDS = frozenset(f"case-{n:03d}" for n in range(1, 16))
"""15건 규모 시점의 케이스. 원천별 집계를 기존/신규로 나누는 데 쓴다."""

MIN_TYPE_CASES_FOR_RATIO = 3
"""`impact_eval._by_type` 과 같은 값. 두 곳에 있는 것은 중복이지만, 한쪽만 고치면
러너와 집계기가 다른 기준으로 「판정 불가」를 정하게 된다는 사실이 드러나야 한다."""


RESULT_GLOB = "impact-claude-*-2*.json"
"""결과 파일 이름 형태. `impact-report.json`(이 스크립트의 출력)을 빨아들이지 않도록
모델명과 타임스탬프를 함께 요구한다."""


def latest_result() -> Path:
    """가장 최근 결과 파일. 없으면 즉시 실패한다 — 빈 집계를 내지 않는다.

    **수정 시각으로 고른다.** 파일명 정렬로 골랐더니 `-deanchored-` 가 들어간 옛 파일이
    사전순 뒤라 뽑혔고, 그 파일에는 캐시 지표가 없어 집계가 죽었다. 죽은 것이 다행이며,
    조용히 옛 실행을 집계했으면 **어느 실행의 수치인지 모르는 표**가 나왔을 것이다.
    """
    files = sorted(RESULTS_DIR.glob(RESULT_GLOB), key=lambda p: p.stat().st_mtime)
    if not files:
        msg = f"{RESULTS_DIR}: 결과 파일이 없다"
        raise SystemExit(msg)
    return files[-1]


def paragraph_map(conn: psycopg.Connection[Any]) -> dict[str, tuple[str, int]]:
    """문단 UUID → (doc_id, article_no). 현재 유효한 행만 본다."""
    rows = conn.execute(
        """
        select p.id::text, d.doc_id, p.article_no
        from policy_paragraph p
        join policy_document d on d.id = p.document_id
        where p.known_until = 'infinity'
        """
    ).fetchall()
    return {str(pid): (str(doc), int(no)) for pid, doc, no in rows}


def retrieved_by_assessment(conn: psycopg.Connection[Any]) -> dict[str, list[str]]:
    """영향평가 ID → 그 호출이 본 검색 결과 문단 UUID 목록.

    같은 평가에 호출이 여러 번이면(재작성) 합집합을 쓴다. 지금은 `MAX_REVISIONS=0`
    이라 한 번이지만, 합집합으로 두어야 재작성을 다시 켰을 때 "첫 호출만 봤다"는
    조용한 축소가 생기지 않는다.
    """
    rows = conn.execute(
        """
        select impact_assessment_id::text, retrieved_chunk_ids
        from llm_invocation
        where impact_assessment_id is not null
          and purpose = 'IMPACT_ASSESSMENT'
          and retrieved_chunk_ids is not null
        """
    ).fetchall()
    out: dict[str, list[str]] = {}
    for aid, chunks in rows:
        out.setdefault(str(aid), []).extend(str(c) for c in chunks)
    return out


def split_a_b(
    cases: list[dict[str, Any]],
    retrieved: dict[str, list[str]],
    mapping: dict[str, tuple[str, int]],
) -> dict[str, Any]:
    """정답 문단 하나하나를 (a) 검색 부재 / (b) 검색됐으나 미인용 / 정상 으로 가른다.

    분모는 **정답 문단 수**이지 케이스 수가 아니다. 케이스 단위로 세면 정답이 3개인
    케이스와 1개인 케이스가 같은 무게를 갖는다.
    """
    per_case: list[dict[str, Any]] = []
    tally = Counter[str]()
    unknown_chunks = 0

    for row in cases:
        expected = set(row.get("expected") or [])
        if not expected:
            per_case.append({"case_id": row["case_id"], "verdict": "NOT_APPLICABLE"})
            continue

        aid = str(row.get("assessment_id") or "")
        chunks = retrieved.get(aid)
        if chunks is None:
            per_case.append({"case_id": row["case_id"], "verdict": "NO_RECORD"})
            tally["NO_RECORD"] += len(expected)
            continue

        found: set[str] = set()
        for chunk in chunks:
            key = mapping.get(chunk)
            if key is None:
                unknown_chunks += 1
                continue
            found.add(f"{key[0]}#{key[1]}")

        cited = set(row.get("cited") or [])
        a = sorted(expected - found)
        b = sorted((expected & found) - cited)
        ok = sorted(expected & found & cited)
        tally["A_NOT_RETRIEVED"] += len(a)
        tally["B_RETRIEVED_NOT_CITED"] += len(b)
        tally["OK"] += len(ok)
        per_case.append(
            {
                "case_id": row["case_id"],
                "verdict": "SCORED",
                "expected": sorted(expected),
                "retrieved_hits": sorted(expected & found),
                "a_not_retrieved": a,
                "b_retrieved_not_cited": b,
                "ok": ok,
                "retrieved_total": len(found),
            }
        )

    scored = tally["A_NOT_RETRIEVED"] + tally["B_RETRIEVED_NOT_CITED"] + tally["OK"]
    return {
        "denominator": "정답 문단 수 (케이스 수 아님)",
        "scored_items": scored,
        "counts": dict(tally),
        "ratios": (
            {k: round(v / scored, 4) for k, v in tally.items() if k != "NO_RECORD"}
            if scored
            else None
        ),
        "unknown_chunks": unknown_chunks,
        "per_case": per_case,
    }


def a_b_breakdown(ab: dict[str, Any], golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """(a)/(b)/OK 를 유형별·시기별로 다시 가른다.

    목적:
        전체 비율이 어느 유형에서 나온 것인지를 낸다. **전체 0.46/0.26 은 두 개의
        다른 분포를 평균한 값**이며 어느 쪽도 대표하지 않는다.

    구현 이유:
        `split_a_b` 의 `per_case` 를 재집계한다. 다시 계산하지 않는 이유는 두 곳에서
        센 값이 어긋나면 어느 쪽이 맞는지 알 수 없기 때문이다 — 같은 사실은 한 번만
        센다.

    트레이드오프:
        정답 문단이 없는 유형(영향 없음 6종)은 칸이 아예 생기지 않는다. 0/0 을 만들면
        "돌리지 않았다"와 "돌렸는데 0건"이 같아진다. 대신 `not_applicable_types` 에
        그 유형들을 이름으로 남긴다.

    엣지 케이스:
        - `verdict` 가 `NO_RECORD`: 분모에서 뺀다. 호출 기록이 없다는 것은 측정하지
          못했다는 뜻이지 못 찾았다는 뜻이 아니다
        - 유형에 채점 항목이 하나도 없음: 칸을 만들지 않는다
    """
    scored = [c for c in ab["per_case"] if c["verdict"] == "SCORED"]
    na_types = sorted(
        {
            str(golden[c["case_id"]]["case_type"])
            for c in ab["per_case"]
            if c["verdict"] == "NOT_APPLICABLE"
        }
    )

    def tally(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        ok = sum(len(r["ok"]) for r in rows)
        a = sum(len(r["a_not_retrieved"]) for r in rows)
        b = sum(len(r["b_retrieved_not_cited"]) for r in rows)
        n = ok + a + b
        if not n:
            return None
        return {
            "items": n,
            "cases": len(rows),
            "OK": ok,
            "A_NOT_RETRIEVED": a,
            "B_RETRIEVED_NOT_CITED": b,
            "ratios": {
                "OK": round(ok / n, 4),
                "A_NOT_RETRIEVED": round(a / n, 4),
                "B_RETRIEVED_NOT_CITED": round(b / n, 4),
            },
        }

    by_type: dict[str, Any] = {}
    for row in scored:
        by_type.setdefault(str(golden[row["case_id"]]["case_type"]), []).append(row)

    return {
        "note": (
            "정답 문단이 있는 케이스만 분모에 든다. 「영향 없음」 유형은 정답 문단이 "
            "없으므로 (a)/(b) 가 정의되지 않는다 — 「케이스에 해당 없음」이다"
        ),
        "not_applicable_types": na_types,
        "by_type": {k: tally(v) for k, v in sorted(by_type.items())},
        "by_era": {
            "existing": tally([r for r in scored if r["case_id"] in EXISTING_IDS]),
            "new": tally([r for r in scored if r["case_id"] not in EXISTING_IDS]),
        },
    }


def by_source(cases: list[dict[str, Any]], golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """원천(MST)별 정확도. 기존/신규를 나눠 센다."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in cases:
        case = golden[row["case_id"]]
        mst = str(case["source"]["mst"])
        era = "existing" if row["case_id"] in EXISTING_IDS else "new"
        entry = buckets.setdefault(
            mst, {"law_name": case["source"]["law_name"], "existing": [], "new": []}
        )
        wants = row["expected_outcome"] == "IMPACT"
        found = row["impact_status"] != "INSUFFICIENT_EVIDENCE"
        entry[era].append(
            {
                "case_id": row["case_id"],
                "expected_outcome": row["expected_outcome"],
                "correct": found is wants,
                "hit": bool(row["hit"]),
            }
        )
    for entry in buckets.values():
        rows = entry["existing"] + entry["new"]
        entry["n"] = len(rows)
        entry["correct"] = sum(1 for r in rows if r["correct"])
        entry["all_same"] = len({r["correct"] for r in rows}) == 1 and len(rows) > 1
    return dict(sorted(buckets.items()))


def impact_without_concentration(
    cases: list[dict[str, Any]], golden: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """편중 원천 두 개를 뺀 IMPACT 정확도.

    **전체 정확도만으로는 편중이 결과를 얼마나 끌었는지 보이지 않는다.**
    IMPACT 20건 중 15건이 285199·283839 에서 나온다.
    """
    out: dict[str, Any] = {}
    for label, keep in (("all", None), ("excluding_concentrated", CONCENTRATED_SOURCES)):
        rows = [
            r
            for r in cases
            if r["expected_outcome"] == "IMPACT"
            and (keep is None or str(golden[r["case_id"]]["source"]["mst"]) not in keep)
        ]
        found = sum(1 for r in rows if r["impact_status"] != "INSUFFICIENT_EVIDENCE")
        hit = sum(1 for r in rows if r["hit"])
        out[label] = {
            "n": len(rows),
            "with_evidence": found,
            "hit_any": hit,
            "with_evidence_ratio": round(found / len(rows), 4) if rows else None,
            "hit_ratio": round(hit / len(rows), 4) if rows else None,
            "verdict": "OK" if len(rows) >= MIN_TYPE_CASES_FOR_RATIO else "TOO_FEW",
            "cases": sorted(r["case_id"] for r in rows),
        }
    return out


def b1_contrast(cases: list[dict[str, Any]], golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """B-1 이 잡은 것과 우리가 인용한 것을 대조한다.

    B-1 은 조 번호 문자열만 본다. 그것이 잡은 문단 중 우리가 놓친 것과 그 반대를
    세면, 문자열 베이스라인과 의미 검색이 **서로 다른 곳에서 틀리는지**가 보인다.
    """
    kinds: dict[str, dict[str, Any]] = {}
    b1_only = 0
    ours_only = 0
    both = 0
    for row in cases:
        case = golden[row["case_id"]]
        b1 = set(case.get("b1_matched_articles") or [])
        cited = set(row.get("cited") or [])
        expected = set(row.get("expected") or [])
        b1_only += len((b1 & expected) - cited)
        ours_only += len((cited & expected) - b1)
        both += len(b1 & cited & expected)

        kind = case.get("b1_match_kind") or "NOT_MATCHED"
        entry = kinds.setdefault(kind, {"n": 0, "correct": 0, "cases": []})
        wants = row["expected_outcome"] == "IMPACT"
        found = row["impact_status"] != "INSUFFICIENT_EVIDENCE"
        entry["n"] += 1
        entry["correct"] += 1 if found is wants else 0
        entry["cases"].append(row["case_id"])

    for entry in kinds.values():
        entry["ratio"] = (
            round(entry["correct"] / entry["n"], 4)
            if entry["n"] >= MIN_TYPE_CASES_FOR_RATIO
            else None
        )
        entry["verdict"] = "OK" if entry["n"] >= MIN_TYPE_CASES_FOR_RATIO else "TOO_FEW"

    return {
        "b1_hit_we_missed": b1_only,
        "we_hit_b1_missed": ours_only,
        "both_hit": both,
        "note": "분모는 정답 문단이다. B-1 이 걸린 문단 중 정답이 아닌 것은 여기 안 센다",
        "by_match_kind": dict(sorted(kinds.items())),
    }


def by_difficulty(cases: list[dict[str, Any]], golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """난이도별 재현율. 난이도는 케이스 파일의 `expected_impacts[].difficulty` 다."""
    tally: dict[str, Counter[str]] = {}
    for row in cases:
        case = golden[row["case_id"]]
        cited = set(row.get("cited") or [])
        for impact in case.get("expected_impacts") or []:
            level = str(impact["difficulty"])
            number = int(str(impact["article_spec"]).split("조")[0].removeprefix("제"))
            key = f"{impact['doc_id']}#{number}"
            counter = tally.setdefault(level, Counter())
            counter["total"] += 1
            counter["hit"] += 1 if key in cited else 0
    return {
        level: {
            "total": c["total"],
            "hit": c["hit"],
            "recall": round(c["hit"] / c["total"], 4) if c["total"] else None,
        }
        for level, c in sorted(tally.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="42건 결과 집계")
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "impact-report.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()

    path = args.result or latest_result()
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data["cases"]
    summary = data["summary"]

    golden_dir = REPO_ROOT / "evals" / "datasets" / "golden"
    golden = {
        c["id"]: c
        for c in (
            yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in sorted(golden_dir.glob("case-*.yaml"))
        )
    }

    try:
        with psycopg.connect(role_dsn(DbRole.GRAPH)) as conn:
            ab = split_a_b(cases, retrieved_by_assessment(conn), paragraph_map(conn))
    except psycopg.Error as exc:
        ab = {"ratios": None, "reason": "DB_UNAVAILABLE", "detail": str(exc)}

    report = {
        "result_file": path.name,
        "model": data["model"],
        "cases": len(cases),
        "by_type": summary.get("by_type"),
        "by_source": by_source(cases, golden),
        "impact_concentration": impact_without_concentration(cases, golden),
        "b1_contrast": b1_contrast(cases, golden),
        "by_difficulty": by_difficulty(cases, golden),
        "a_vs_b": ab,
        "a_vs_b_breakdown": a_b_breakdown(ab, golden)
        if "per_case" in ab
        else {"reason": ab.get("reason")},
        # 옛 실행 파일에는 캐시 지표가 없다. `.get` 으로 None 을 내되 0 으로 채우지
        # 않는다 — "캐시를 안 썼다"와 "그 실행은 캐시를 재지 않았다"는 다른 사실이다.
        "cost": {
            "estimated_cost_usd": summary.get("estimated_cost_usd"),
            "cache_hit_ratio": summary.get("cache_hit_ratio"),
            "billed_input_tokens": summary.get("billed_input_tokens"),
            "output_tokens": summary.get("output_tokens"),
        },
    }
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8"
    )
    logger.info("(a)/(b) 건수: %s", json.dumps(report["a_vs_b"].get("counts"), ensure_ascii=False))
    logger.info("(a)/(b) 비율: %s", json.dumps(report["a_vs_b"].get("ratios"), ensure_ascii=False))
    logger.info("결과: %s", args.out)


if __name__ == "__main__":
    main()
