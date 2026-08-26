"""위임 승격의 `top_n` 을 골든셋으로 정한다 — 재현율과 정밀도를 **함께** 본다 (R-22).

    uv run --group eval python -m evals.runners.delegation_sweep

무엇을 재는가:

1. **R-22 3항목**(`ISP-POL-001` 제5·8·15조)이 N 별로 몇 개 회수되는가.
2. **승격분이 만든 오탐** — 올라온 문단 중 정답이 아닌 것, 특히 골든셋이 심어 둔 decoy.
3. 전체 재현율(정답 26항목)과 후보 수의 변화.

**N 을 재현율만 보고 고르지 않는다.** 재현율만 보면 N 은 항상 커지는 쪽이 좋고, 그러면
검토 큐가 소음으로 찬다 — ADR-003 의 「틀렸음을 알게 되는 신호 1번」이 그 상태다.

승격은 **1차 검색을 고치지 않는다.** N=0 줄이 3단계 기준선과 같은 수치여야 하며, 다르면
승격 코드가 1차 결과를 건드린 것이다. 그 확인이 이 러너의 첫 번째 일이다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.runners.policy_index import build_client
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv
from regchange.guards.killswitch import SwitchGate
from regchange.retrieval import build_query, parse_article_spec
from regchange.retrieval.models import RetrievalSource, SearchMode
from regchange.retrieval.promote import load_delegation_graph, promote_by_delegation
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

DECISION_K = 10
AS_OF = dt.date(2026, 2, 1)
EMBEDDING = "kure-v1"
SWEEP = (0, 1, 2, 3)

R22_ITEMS = {("ISP-POL-001", 5), ("ISP-POL-001", 8), ("ISP-POL-001", 15)}
"""1차 검색이 13·82·72위로 밀어낸 정책 계층 조항 3항목.

`docs/11-obligation-extraction-baseline.md` §5.2 와 R-22 의 관측표에서 그대로 가져왔다.
이 집합을 여기서 늘리지 않는다 — 결과를 보고 대상을 고르면 지표가 의미를 잃는다."""

logger = logging.getLogger("delegation_sweep")


def _refs(entries: list[dict[str, Any]] | None) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for entry in entries or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        out.add((str(entry["doc_id"]), number))
    return out


async def run(top_ns: tuple[int, ...]) -> None:
    cases = [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(GOLDEN_DIR.glob("case-*.yaml"))
    ]
    client = build_client(EMBEDDING)
    rows: list[dict[str, Any]] = []

    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        graph = await load_delegation_graph(conn, as_of=AS_OF)
        logger.info(
            "위임 간선 %d개 | 선언 없음 %s | 제1조 없음 %s | 상위 문서 부재 %d",
            len(graph.edges),
            graph.undeclared or "없음",
            graph.missing_article or "없음",
            len(graph.dangling),
        )
        for edge in graph.edges:
            logger.info(
                "  %s → %s %s",
                edge.child_doc_id,
                edge.parent_doc_id,
                f"제{edge.parent_article_no}조" if edge.parent_article_no else "(문서 단위)",
            )

        for case in cases:
            source = case["source"]
            query = build_query(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                after_text=str(source["after"]),
            )
            base = await search(
                conn,
                switches=switches,
                query=query,
                mode=SearchMode.HYBRID,
                limit=DECISION_K,
                as_of=AS_OF,
                client=client,
            )
            expected = _refs(case.get("expected_impacts"))
            decoys = _refs(case.get("decoys"))

            for top_n in top_ns:
                if top_n == 0:
                    result = base
                else:
                    result = await promote_by_delegation(
                        conn,
                        switches=switches,
                        result=base,
                        query=query,
                        as_of=AS_OF,
                        top_n=top_n,
                        client=client,
                        mode=SearchMode.HYBRID,
                        graph=graph,
                    )
                keys = [c.key for c in result.chunks]
                promoted = [
                    c for c in result.chunks if c.source is RetrievalSource.DELEGATION_PROMOTED
                ]
                promoted_keys = [c.key for c in promoted]
                rows.append(
                    {
                        "case_id": case["id"],
                        "outcome": str(case["expected_outcome"]),
                        "top_n": top_n,
                        "candidates": len(keys),
                        "expected_total": len(expected),
                        "hits": sorted(f"{d}#{n}" for d, n in (set(keys) & expected)),
                        "r22_hits": sorted(
                            f"{d}#{n}" for d, n in (set(keys) & expected & R22_ITEMS)
                        ),
                        "promoted": sorted(f"{d}#{n}" for d, n in promoted_keys),
                        "promoted_correct": sorted(
                            f"{d}#{n}" for d, n in (set(promoted_keys) & expected)
                        ),
                        "promoted_decoy": sorted(
                            f"{d}#{n}" for d, n in (set(promoted_keys) & decoys)
                        ),
                        "promoted_unrelated": sorted(
                            f"{d}#{n}" for d, n in (set(promoted_keys) - expected - decoys)
                        ),
                    }
                )

    _report(rows, top_ns)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"delegation-sweep-{stamp}.json"
    out.write_text(
        json.dumps(
            {"as_of": AS_OF.isoformat(), "k": DECISION_K, "cases": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("결과: %s", out)


def _report(rows: list[dict[str, Any]], top_ns: tuple[int, ...]) -> None:
    """규약 §4 의 지표로 낸다 — 재현율@k 는 **IMPACT 10건의 케이스별 평균**이다.

    `expected_impacts` 가 빈 5건에는 재현율을 계산하지 않는다(규약 §5). 항목 수로
    미시 평균을 내면 정답이 많은 케이스가 지표를 지배하고, 3단계 기준선(0.7667)과
    비교할 수 없게 된다.
    """
    logger.info("─── N 별 요약 (재현율@k = IMPACT 10건 케이스별 평균, 규약 §4) ───")
    logger.info(
        "%3s %8s %10s %9s %9s %9s %9s",
        "N",
        "후보평균",
        "재현율",
        "R-22회수",
        "승격계",
        "승격정답",
        "승격오탐",
    )
    for top_n in top_ns:
        subset = [r for r in rows if r["top_n"] == top_n]
        scored = [r for r in subset if r["expected_total"]]
        recall = (
            sum(len(r["hits"]) / r["expected_total"] for r in scored) / len(scored)
            if scored
            else 0.0
        )
        promoted = sum(len(r["promoted"]) for r in subset)
        correct = sum(len(r["promoted_correct"]) for r in subset)
        decoy = sum(len(r["promoted_decoy"]) for r in subset)
        unrelated = sum(len(r["promoted_unrelated"]) for r in subset)
        r22 = sum(len(r["r22_hits"]) for r in subset)
        avg = sum(r["candidates"] for r in subset) / len(subset)
        logger.info(
            "%3d %8.1f %10.4f %9s %9d %9d %9s",
            top_n,
            avg,
            recall,
            f"{r22}/3",
            promoted,
            correct,
            f"{decoy} decoy + {unrelated} 무관",
        )

    logger.info("─── 승격이 실제로 올린 문단 (케이스별) ───")
    for row in rows:
        if row["promoted"]:
            logger.info(
                "N=%d %s: %s  [정답 %s / decoy %s]",
                row["top_n"],
                row["case_id"],
                ", ".join(row["promoted"]),
                row["promoted_correct"] or "-",
                row["promoted_decoy"] or "-",
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()
    parser = argparse.ArgumentParser(description="위임 승격 top_n 스윕")
    parser.add_argument("--top-n", default=",".join(str(n) for n in SWEEP))
    args = parser.parse_args()
    top_ns = tuple(int(v) for v in args.top_n.split(",") if v.strip())
    asyncio.run(run(top_ns))


if __name__ == "__main__":
    main()
