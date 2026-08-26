"""단순 베이스라인 3종 — **재현율 0.8167 이 좋은 값인지** 알기 위한 대조군 (6단계 §2).

    uv run --group eval python -m evals.runners.baseline

무엇을 재는가:

| 이름 | 무엇인가 | 무엇을 시험하는가 |
|---|---|---|
| **B-1** | 개정 조문의 조 번호를 사내 규정 본문에서 문자열로 찾는다 | EASY 가 우리 성과인가 |
| **B-2** | 152개 조에서 무작위 k개 | 하한선. 이보다 못하면 검색이 해를 끼친 것이다 |
| **B-3** | 검색 top-k 를 **그대로** 영향 문단으로 삼는다 | LLM 이 검색 위에 무엇을 더했는가 |

**LLM 을 부르지 않는다.** 비용 0, B-1·B-3 은 결정론적이다.

규약을 고치지 않는다 (`docs/10-retrieval-evaluation-protocol.md`): k=10, HYBRID,
KURE-v1, `as_of=2026-02-01`. B-3 은 운영 설정과 같게 위임 승격을 켠 값도 함께 낸다.

**베이스라인이 높게 나와도 실패가 아니다.** 그것은 우리 코퍼스의 성질이며, 아는 것이
모르는 것보다 낫다. 다만 그 경우 골든셋 확장에서 문자열 매칭으로 안 잡히는 케이스를
늘려야 한다 — 그 판단의 근거가 이 측정이다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.runners.policy_index import build_client as build_embedding
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv
from regchange.guards.killswitch import SwitchGate
from regchange.retrieval import build_query, parse_article_spec
from regchange.retrieval.models import SearchMode
from regchange.retrieval.promote import DEFAULT_TOP_N, promote_by_delegation
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

AS_OF = dt.date(2026, 2, 1)
TOP_K = 10
EMBEDDING = "kure-v1"

RANDOM_SEED = 20260822
"""B-2 의 시드. 고정하는 이유는 재현이고, 값 자체에 의미는 없다 — 측정일이다."""

RANDOM_TRIALS = 200
"""B-2 반복 횟수. 152C10 의 분포를 15케이스 평균으로 보려면 수백 회면 충분하고,
그 이상은 소수점 셋째 자리를 다듬을 뿐이다."""

PARAGRAPH_QUERY = """
SELECT d.doc_id, p.article_no, p.text_raw
  FROM policy_paragraph p
  JOIN policy_document d ON d.id = p.document_id
 WHERE p.known_until = 'infinity' AND d.known_until = 'infinity'
   AND d.effective_date <= %(as_of)s
 ORDER BY d.doc_id, p.seq_in_doc
"""

logger = logging.getLogger("baseline")


def article_tokens(article_path: str) -> list[str]:
    """개정 조문 경로에서 **사내 규정이 인용했을 법한 조 표기**를 뽑는다.

    목적:
        B-1 이 찾을 문자열을 만든다.

    구현 이유:
        골든셋의 `article_path` 는 사람이 쓴 표기라 형태가 여럿이다 —
        `제48조의3`, `제45조의4·5`, `제44조의2~5`, `제25조의2 외`. 범위·나열을 펼치지
        않으면 B-1 이 실제보다 약해 보이고, **베이스라인을 약하게 잡으면 우리 기여가
        부풀려진다.** 그래서 펼치는 쪽으로 만든다.

    트레이드오프:
        조 번호만 보고 법령명을 보지 않는다. 사내 규정이 다른 법의 같은 조 번호를
        인용했다면 오탐이다. 그 방향도 베이스라인을 **강하게** 만들므로 우리에게
        불리한 쪽이고, 그래서 허용한다.

    엣지 케이스:
        - `제44조의2~5`: 2,3,4,5 로 펼친다.
        - `제45조의4·5`: 4,5 로 펼친다.
        - `제25조의2 외`: "외" 는 버린다. 무엇이 더 있는지 골든셋이 적지 않았다.
        - 조 번호를 못 읽음: 빈 목록. 그 케이스는 B-1 이 아무것도 못 찾은 것으로 센다.
    """
    text = article_path.strip()
    out: list[str] = []
    for match in re.finditer(r"제(\d+)조(?:의(\d+))?((?:\s*[·,~-]\s*\d+)*)", text):
        base, sub, tail = match.group(1), match.group(2), match.group(3) or ""
        out.append(f"제{base}조" + (f"의{sub}" if sub else ""))
        numbers = [int(n) for n in re.findall(r"\d+", tail)]
        if not numbers or sub is None:
            continue
        if "~" in tail or "-" in tail:
            for n in range(int(sub) + 1, max(numbers) + 1):
                out.append(f"제{base}조의{n}")
        else:
            out.extend(f"제{base}조의{n}" for n in numbers)
    return list(dict.fromkeys(out))


def _refs(entries: list[dict[str, Any]] | None) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for entry in entries or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        out.add((str(entry["doc_id"]), number))
    return out


def _difficulty_items(case: dict[str, Any]) -> list[tuple[str, tuple[str, int]]]:
    items = []
    for entry in case.get("expected_impacts") or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        items.append((str(entry.get("difficulty", "UNKNOWN")), (str(entry["doc_id"]), number)))
    return items


def load_cases() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(GOLDEN_DIR.glob("case-*.yaml"))
    ]


def score(
    name: str,
    cases: list[dict[str, Any]],
    claims: dict[str, list[tuple[str, int]]],
) -> dict[str, Any]:
    """케이스별 「주장한 문단 목록」을 골든셋과 대조한다.

    세 베이스라인과 LLM 파이프라인이 **같은 함수로 채점된다.** 채점기가 다르면
    비교가 성립하지 않는다.
    """
    impact_cases = [c for c in cases if c["expected_outcome"] == "IMPACT"]
    empty_cases = [c for c in cases if c["expected_outcome"] == "INSUFFICIENT_EVIDENCE"]
    no_impact = [c for c in cases if c["expected_outcome"] == "NO_IMPACT"]

    hit_cases = 0
    recall_sum = 0.0
    precision_sum = 0.0
    decoys_cited = 0
    claimed_total = 0
    by_difficulty: Counter[str] = Counter()
    by_difficulty_hit: Counter[str] = Counter()
    hard_cases = 0
    hard_covered = 0

    for case in cases:
        claimed = set(claims.get(str(case["id"]), []))
        claimed_total += len(claimed)
        expected = _refs(case.get("expected_impacts"))
        decoys = _refs(case.get("decoys"))
        decoys_cited += len(claimed & decoys)

        for difficulty, ref in _difficulty_items(case):
            by_difficulty[difficulty] += 1
            if ref in claimed:
                by_difficulty_hit[difficulty] += 1

        if case["expected_outcome"] != "IMPACT":
            continue
        if claimed & expected:
            hit_cases += 1
        recall_sum += len(claimed & expected) / len(expected) if expected else 0.0
        precision_sum += len(claimed & expected) / len(claimed) if claimed else 0.0
        docs = {doc for doc, _ in expected}
        if len(docs) >= 2:
            hard_cases += 1
            if len({doc for doc, _ in (claimed & expected)}) >= 2:
                hard_covered += 1

    # 베이스라인은 "모른다"를 말할 수 없다 — 후보가 있으면 언제나 내놓는다.
    abstained_empty = sum(1 for c in empty_cases if not claims.get(str(c["id"])))
    abstained_no_impact = sum(1 for c in no_impact if not claims.get(str(c["id"])))

    return {
        "name": name,
        "impact_cases": len(impact_cases),
        "case_hit": hit_cases,
        "item_recall": round(recall_sum / len(impact_cases), 4) if impact_cases else None,
        "precision": round(precision_sum / len(impact_cases), 4) if impact_cases else None,
        "claimed_per_case": round(claimed_total / len(cases), 2) if cases else None,
        "decoys_cited": decoys_cited,
        "hard_cases": hard_cases,
        "hard_both_docs": hard_covered,
        "difficulty_recall": {
            level: round(by_difficulty_hit[level] / total, 4)
            for level, total in sorted(by_difficulty.items())
        },
        "difficulty_n": dict(sorted(by_difficulty.items())),
        "empty_abstained": abstained_empty,
        "empty_total": len(empty_cases),
        "no_impact_abstained": abstained_no_impact,
        "no_impact_total": len(no_impact),
    }


def claims_from_impact(path: Path) -> tuple[dict[str, list[tuple[str, int]]], dict[str, Any]]:
    """LLM 파이프라인 결과 파일을 **같은 채점기가 읽을 수 있는 형태**로 바꾼다.

    목적:
        베이스라인과 파이프라인을 한 채점 함수로 비교한다.

    구현 이유:
        러너마다 다른 채점기를 쓰면 "0.8167 대 0.30" 같은 문장이 성립하지 않는다.
        결과 파일의 `cited`(`ISP-PROC-002#7` 형태)만 읽어 같은 함수에 넣는다.

    트레이드오프:
        인용 목록만 본다 — 부서·위험도·상태는 이 비교에 들어가지 않는다. 그 값들은
        베이스라인에 대응물이 없어서 비교가 성립하지 않는다.

    엣지 케이스:
        - **무효 실행**(`valid=false` 또는 토큰 0): `ValueError`. 실패한 실행을 베이스라인과
          비교하면 "LLM 이 아무것도 못 한다"는 결론이 나오고, 그것은 사실이 아니다
          (`docs/incidents/measurement-reported-failure-as-success.md`).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    if summary.get("valid") is False or not summary.get("input_tokens"):
        msg = f"{path.name}: 무효 실행이다. 베이스라인 비교에 쓸 수 없다"
        raise ValueError(msg)
    claims: dict[str, list[tuple[str, int]]] = {}
    for row in data["cases"]:
        refs = []
        for entry in row.get("cited") or []:
            doc, _, number = str(entry).partition("#")
            refs.append((doc, int(number)))
        claims[str(row["case_id"])] = refs
    return claims, summary


async def run(impact_result: Path | None) -> None:
    apply_dotenv()
    cases = load_cases()
    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))
    embedding = build_embedding(EMBEDDING)

    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        cur = await conn.execute(PARAGRAPH_QUERY, {"as_of": AS_OF})
        corpus = [(str(d), int(n), str(t)) for d, n, t in await cur.fetchall()]

        # ── B-1. 법령 인용 문자열 매칭 ────────────────────────────────
        b1: dict[str, list[tuple[str, int]]] = {}
        b1_tokens: dict[str, list[str]] = {}
        for case in cases:
            tokens = article_tokens(str(case["source"].get("article_path") or ""))
            b1_tokens[str(case["id"])] = tokens
            matched = [
                (doc, no) for doc, no, text in corpus if any(token in text for token in tokens)
            ]
            b1[str(case["id"])] = matched[:TOP_K]

        # ── B-2. 무작위 k개 ──────────────────────────────────────────
        rng = random.Random(RANDOM_SEED)  # noqa: S311 — 암호 용도가 아니라 재현용 시드다
        refs = [(doc, no) for doc, no, _ in corpus]
        trials: list[dict[str, Any]] = []
        for _ in range(RANDOM_TRIALS):
            picked = {str(c["id"]): rng.sample(refs, TOP_K) for c in cases}
            trials.append(score("B-2", cases, picked))
        b2 = {
            key: round(sum(t[key] for t in trials) / len(trials), 4)
            for key in ("case_hit", "item_recall", "precision", "decoys_cited")
        }
        b2["trials"] = RANDOM_TRIALS
        b2["name"] = "B-2"

        # ── B-3. 검색 top-k 를 그대로 ────────────────────────────────
        b3: dict[str, list[tuple[str, int]]] = {}
        b3p: dict[str, list[tuple[str, int]]] = {}
        for case in cases:
            source = case["source"]
            query = build_query(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                after_text=str(source["after"]),
            )
            result = await search(
                conn,
                switches=switches,
                query=query,
                mode=SearchMode.HYBRID,
                limit=TOP_K,
                as_of=AS_OF,
                client=embedding,
            )
            b3[str(case["id"])] = [(c.doc_id, c.article_no) for c in result.chunks]
            promoted = await promote_by_delegation(
                conn,
                switches=switches,
                result=result,
                query=query,
                as_of=AS_OF,
                top_n=DEFAULT_TOP_N,
                client=embedding,
                mode=SearchMode.HYBRID,
            )
            b3p[str(case["id"])] = [(c.doc_id, c.article_no) for c in promoted.chunks]

    llm_row = None
    if impact_result is not None:
        claims, llm_summary = claims_from_impact(impact_result)
        llm_row = score(f"LLM 파이프라인 ({impact_result.name})", cases, claims) | {
            "source": impact_result.name,
            "empty_abstained_actual": llm_summary.get("empty_correct"),
        }

    report = {
        "protocol": "docs/10-retrieval-evaluation-protocol.md",
        "as_of": AS_OF.isoformat(),
        "k": TOP_K,
        "corpus_paragraphs": len(corpus),
        "B-1": score("B-1 인용 문자열", cases, b1)
        | {"tokens": b1_tokens, "matches": {k: len(v) for k, v in b1.items()}},
        "B-2": b2,
        "B-3": score("B-3 검색 top-k", cases, b3),
        "B-3-promoted": score("B-3 검색 top-k + 위임승격", cases, b3p),
        "LLM": llm_row,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"baseline-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for key in ("B-1", "B-3", "B-3-promoted", "LLM"):
        if report[key] is None:
            continue
        row: dict[str, Any] = report[key]  # type: ignore[assignment]
        logger.info(
            "%-22s 케이스적중 %2d/%d | 항목재현율 %s | 정밀도 %s | 난이도별 %s | decoy %d | "
            "케이스당 주장 %s | EMPTY 기권 %d/%d",
            row["name"],
            row["case_hit"],
            row["impact_cases"],
            row["item_recall"],
            row["precision"],
            row["difficulty_recall"],
            row["decoys_cited"],
            row["claimed_per_case"],
            row["empty_abstained"],
            row["empty_total"],
        )
    logger.info("B-2 무작위(%d회 평균): %s", RANDOM_TRIALS, b2)
    logger.info("결과: %s", out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="단순 베이스라인 3종 (6단계 §2)")
    parser.add_argument(
        "--impact-result",
        type=Path,
        default=None,
        help="LLM 파이프라인 결과 파일. 같은 채점기로 함께 채점한다",
    )
    args = parser.parse_args()
    asyncio.run(run(args.impact_result))


if __name__ == "__main__":
    main()
