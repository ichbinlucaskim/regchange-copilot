"""검색 품질 채점 — `docs/10-retrieval-evaluation-protocol.md` 를 그대로 구현한다.

규약은 측정 **이전에** 고정됐다. 이 파일은 그 규약을 코드로 옮긴 것이며, 지표 정의나
k 값을 여기서 바꾸면 규약 문서의 §7 에 변경 이력을 남겨야 한다.

    uv run python -m evals.runners.retrieval_eval --embed bge-m3 --mode vector
    uv run python -m evals.runners.retrieval_eval --embed kure-v1 --mode all

결과는 `evals/results/` 에 JSON 으로 남기고 표를 표준출력에 찍는다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.runners.policy_index import LOCAL_MODELS, build_client
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv
from regchange.guards.killswitch import SwitchGate
from regchange.retrieval import build_query, parse_article_spec
from regchange.retrieval.models import SearchMode
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

DECISION_K = 10
"""결정 지표의 k. 규약 §3 — 하한 4(케이스 최대 정답 수), 코퍼스 152조의 6.6%."""

REFERENCE_K = 5
"""참고로 함께 보고하는 k. 선택의 근거로 삼지 않는다."""

AS_OF = dt.date(2026, 2, 1)
"""검색 시점. 코퍼스 문서의 `effective_date` 상한과 같은 값이며, 골든셋의 가장 이른
시행일(2026-08-13)보다 앞선다. 오늘 날짜를 쓰지 않는 이유: 측정이 실행일에 따라
달라지면 재현이 깨진다."""

BOUNDARY_CASES = {("case-002", "ISP-GUIDE-002", 11)}
"""규약 §6 — 오답으로 세지 않고 별도 줄로 보고하는 경계 판정 항목."""

logger = logging.getLogger("retrieval_eval")


@dataclass(slots=True)
class CaseScore:
    """케이스 하나의 채점 결과."""

    case_id: str
    outcome: str
    expected: set[tuple[str, int]]
    decoys: set[tuple[str, int]]
    hits: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    decoy_hits: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    boundary_hits: dict[int, set[tuple[str, int]]] = field(default_factory=dict)
    first_hit_rank: int | None = None
    top_scores: list[float] = field(default_factory=list)
    difficulty_hits: dict[str, bool] = field(default_factory=dict)

    def recall(self, k: int) -> float | None:
        """재현율@k. 정답 집합이 비어 있으면 계산하지 않는다 (규약 §5)."""
        if not self.expected:
            return None
        return len(self.hits[k]) / len(self.expected)

    def precision(self, k: int) -> float | None:
        """정밀도@k. 분모는 k 이며 이론적 상한은 `|expected|/k` 다 (규약 §4)."""
        if not self.expected:
            return None
        return len(self.hits[k]) / k

    def decoy_rate(self, k: int) -> float | None:
        """심어 둔 decoy 중 top-k 에 끌려 들어온 비율."""
        if not self.decoys:
            return None
        return len(self.decoy_hits[k]) / len(self.decoys)


def load_cases() -> list[dict[str, Any]]:
    """골든셋 15건을 파일명 순으로 읽는다."""
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(GOLDEN_DIR.glob("case-*.yaml"))
    ]
    if not cases:
        msg = f"{GOLDEN_DIR}: 골든셋 케이스가 없다"
        raise SystemExit(msg)
    return cases


def _refs(entries: list[dict[str, Any]] | None) -> set[tuple[str, int]]:
    """`expected_impacts` / `decoys` 를 `(doc_id, 조 번호)` 집합으로 바꾼다."""
    out: set[tuple[str, int]] = set()
    for entry in entries or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        out.add((str(entry["doc_id"]), number))
    return out


async def score_case(
    conn: psycopg.AsyncConnection[Any],
    case: dict[str, Any],
    *,
    switches: SwitchGate,
    mode: SearchMode,
    client: Any,
) -> CaseScore:
    """케이스 하나를 검색하고 규약대로 채점한다."""
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
        mode=mode,
        limit=DECISION_K,
        as_of=AS_OF,
        client=client,
    )
    ranked = [chunk.key for chunk in result.chunks]

    expected = _refs(case.get("expected_impacts"))
    decoys = _refs(case.get("decoys"))
    boundary = {
        (doc_id, number) for (case_id, doc_id, number) in BOUNDARY_CASES if case_id == case["id"]
    }
    decoys -= boundary

    score = CaseScore(
        case_id=str(case["id"]),
        outcome=str(case["expected_outcome"]),
        expected=expected,
        decoys=decoys,
        top_scores=[chunk.score for chunk in result.chunks],
    )
    for k in (REFERENCE_K, DECISION_K):
        top = set(ranked[:k])
        score.hits[k] = top & expected
        score.decoy_hits[k] = top & decoys
        score.boundary_hits[k] = top & boundary

    for position, key in enumerate(ranked, start=1):
        if key in expected:
            score.first_hit_rank = position
            break

    top_decision = set(ranked[:DECISION_K])
    for entry in case.get("expected_impacts") or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        difficulty = str(entry.get("difficulty", "UNKNOWN"))
        label = f"{difficulty}|{entry['doc_id']}#{number}"
        score.difficulty_hits[label] = (str(entry["doc_id"]), number) in top_decision
    return score


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(scores: list[CaseScore]) -> dict[str, Any]:
    """규약 §4 의 지표를 케이스 단위 매크로 평균으로 집계한다."""
    impact = [s for s in scores if s.expected]
    empty = [s for s in scores if not s.expected]

    summary: dict[str, Any] = {
        "impact_cases": len(impact),
        "empty_cases": len(empty),
    }
    for k in (REFERENCE_K, DECISION_K):
        summary[f"recall@{k}"] = _mean([s.recall(k) or 0.0 for s in impact])
        summary[f"precision@{k}"] = _mean([s.precision(k) or 0.0 for s in impact])
        rates = [s.decoy_rate(k) for s in scores]
        summary[f"decoy@{k}"] = _mean([r for r in rates if r is not None])
        summary[f"decoy@{k}_empty_cases"] = _mean(
            [r for r in (s.decoy_rate(k) for s in empty) if r is not None]
        )

    summary["mrr"] = _mean([1.0 / s.first_hit_rank if s.first_hit_rank else 0.0 for s in impact])
    summary["hit@1"] = _mean([1.0 if s.first_hit_rank == 1 else 0.0 for s in impact])

    # 난이도별 — 항목 단위 (규약 §4)
    by_difficulty: dict[str, list[bool]] = defaultdict(list)
    for score in impact:
        for label, hit in score.difficulty_hits.items():
            by_difficulty[label.split("|", 1)[0]].append(hit)
    # HARD 는 케이스 성질이다 — 서로 다른 doc_id 2개 이상에 걸친 케이스의 정답 항목.
    hard: list[bool] = []
    for score in impact:
        if len({doc for doc, _ in score.expected}) >= 2:
            hard.extend(score.difficulty_hits.values())
    summary["difficulty"] = {
        name: {"n": len(hits), f"recall@{DECISION_K}": _mean([float(h) for h in hits])}
        for name, hits in sorted(by_difficulty.items())
    }
    summary["difficulty"]["HARD"] = {
        "n": len(hard),
        f"recall@{DECISION_K}": _mean([float(h) for h in hard]),
    }

    summary["boundary_hits"] = sum(len(s.boundary_hits[DECISION_K]) for s in scores)
    summary["top1_score_impact"] = _mean([s.top_scores[0] for s in impact if s.top_scores])
    summary["top1_score_empty"] = _mean([s.top_scores[0] for s in empty if s.top_scores])
    return summary


async def run(embed: str, modes: list[SearchMode]) -> None:
    cases = load_cases()
    client = build_client(embed)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "protocol": "docs/10-retrieval-evaluation-protocol.md",
        "embedding": client.model_id,
        "as_of": AS_OF.isoformat(),
        "decision_k": DECISION_K,
        "modes": {},
    }

    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        for mode in modes:
            scores = [
                await score_case(conn, case, switches=switches, mode=mode, client=client)
                for case in cases
            ]
            summary = summarize(scores)
            report["modes"][mode.value] = {
                "summary": summary,
                "cases": [
                    {
                        "case_id": s.case_id,
                        "outcome": s.outcome,
                        "expected": sorted(f"{d}#{n}" for d, n in s.expected),
                        f"hits@{DECISION_K}": sorted(f"{d}#{n}" for d, n in s.hits[DECISION_K]),
                        f"recall@{DECISION_K}": s.recall(DECISION_K),
                        f"decoy@{DECISION_K}": s.decoy_rate(DECISION_K),
                        "first_hit_rank": s.first_hit_rank,
                        "top_scores": [round(v, 4) for v in s.top_scores],
                    }
                    for s in scores
                ],
            }
            _print_summary(mode, summary)

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"retrieval-{embed}-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("결과: %s", out)


def _print_summary(mode: SearchMode, summary: dict[str, Any]) -> None:
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.4f}"

    logger.info("─── %s ───", mode.value)
    logger.info(
        "재현율@%d=%s  정밀도@%d=%s  DECOY@%d=%s  MRR=%s  hit@1=%s",
        DECISION_K,
        fmt(summary[f"recall@{DECISION_K}"]),
        DECISION_K,
        fmt(summary[f"precision@{DECISION_K}"]),
        DECISION_K,
        fmt(summary[f"decoy@{DECISION_K}"]),
        fmt(summary["mrr"]),
        fmt(summary["hit@1"]),
    )
    logger.info("재현율@%d=%s (참고)", REFERENCE_K, fmt(summary[f"recall@{REFERENCE_K}"]))
    for name, values in summary["difficulty"].items():
        logger.info(
            "  %-8s n=%-3d 재현율@%d=%s",
            name,
            values["n"],
            DECISION_K,
            fmt(values[f"recall@{DECISION_K}"]),
        )
    logger.info(
        "  EMPTY 5건 DECOY@%d=%s / top1 점수 IMPACT=%s EMPTY=%s / 경계 혼입=%d",
        DECISION_K,
        fmt(summary[f"decoy@{DECISION_K}_empty_cases"]),
        fmt(summary["top1_score_impact"]),
        fmt(summary["top1_score_empty"]),
        summary["boundary_hits"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # `.env` 를 프로세스 환경에 채운다 — DSN 과 임베딩 API 키가 거기 있다.
    # 이미 주입된 환경변수는 덮지 않는다 (`apply_dotenv` 의 setdefault).
    apply_dotenv()
    parser = argparse.ArgumentParser(description="검색 품질 채점")
    parser.add_argument("--embed", choices=[*LOCAL_MODELS, "openai"], required=True)
    parser.add_argument("--mode", choices=["vector", "lexical", "hybrid", "all"], default="vector")
    args = parser.parse_args()
    modes = (
        [SearchMode.VECTOR, SearchMode.LEXICAL, SearchMode.HYBRID]
        if args.mode == "all"
        else [SearchMode(args.mode.upper())]
    )
    asyncio.run(run(args.embed, modes))


if __name__ == "__main__":
    main()
