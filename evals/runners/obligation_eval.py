"""의무사항 추출 + 근거 강제 gate 를 골든셋 15건으로 평가하고 **비용을 실측한다**.

    uv run python -m evals.runners.obligation_eval --model sonnet
    uv run python -m evals.runners.obligation_eval --model opus --cases case-005,case-010

무엇을 재는가:

1. **EMPTY 3건(013·014·015)에서 `INSUFFICIENT_EVIDENCE` 가 나오는가.** 3단계의 완료
   조건이며, 이것이 나오지 않으면 "모른다고 말하는 기능"이 없는 것이다.
2. **NO_IMPACT 2건(003·011)에서 근거 없는 주장이 만들어지지 않는가.**
3. **IMPACT 10건에서 gate 를 통과한 인용이 실제 정답 문단을 가리키는가.**
4. **인용 폐기율과 폐기 사유 분포.** gate 가 통과만 시키면 gate 가 아니다 —
   `NOT_RETRIEVED` 와 `QUOTE_NOT_FOUND` 가 각각 몇 건인지가 이 gate 의 작동 증거다.
5. **비용.** 15건 1회 평가가 얼마인지 알아야 6단계에서 50~80건으로 늘릴 때 계산이 선다.

**낮게 나온 케이스만 Opus 로 다시 돌린다.** `--cases` 로 지정하며, 두 모델 기록이
`llm_invocation` 에 남아 나중에 비교할 수 있다 — 같은 케이스에 두 `model_id` 가 있으면
대조가 성립한다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.runners.policy_index import build_client as build_embedding
from regchange.adapters.llm.claude import COMPARISON_MODEL, DEFAULT_MODEL, ClaudeClient
from regchange.adapters.storage.local import LocalDocumentStore
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv, snapshot_root
from regchange.guards.citations import GateStatus
from regchange.guards.killswitch import SwitchGate
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval import parse_article_spec
from regchange.retrieval.models import SearchMode
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

AS_OF = dt.date(2026, 2, 1)
"""검색 시점. 검색 측정과 같은 값이어야 두 측정이 같은 코퍼스를 본다
(`docs/10-retrieval-evaluation-protocol.md`)."""

EMBEDDING = "kure-v1"
"""ADR-015 가 채택한 임베딩. 여기서 바꾸면 검색 결과가 달라져 추출 평가가 오염된다."""

PRICE_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5": {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}
"""100만 토큰당 USD 공표 단가. **2026-08-20 에 공식 문서에서 직접 확인했다.**

정정 이력: 이 상수를 처음 넣을 때 "Sonnet 5 는 2026-08-31 까지 도입 단가이고 이후
$3/$15 로 갱신 필요"라고 적었다. **틀렸다** — 확인 결과 $2/$10 이 정가가 됐고
2026-09-01 예정이던 인상은 취소됐다. 그 알림을 그대로 뒀다면 9월에 근거 없이 단가를
1.5배로 올려 비용을 과대 계상했을 것이다. *상수를 확인 없이 쓰지 않는다*가 값을 한 자리다.

캐시 항목을 함께 둔다. 지금은 프롬프트 캐싱을 쓰지 않아 0 이지만, `llm_invocation` 이
캐시 토큰을 이미 기록하는데 계산식이 그것을 빼면 **기록과 청구액이 조용히 어긋난다.**

단가는 공표값이고 **실측하는 것은 토큰 수**다. 금액은 그 곱이며, 실제 청구액과
대조해야 비로소 검증된다."""

logger = logging.getLogger("obligation_eval")


def load_cases(selected: set[str] | None) -> list[dict[str, Any]]:
    """골든셋을 읽는다. `--cases` 로 일부만 고를 수 있다 (실패 케이스 대조용)."""
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(GOLDEN_DIR.glob("case-*.yaml"))
    ]
    if selected:
        cases = [c for c in cases if c["id"] in selected]
        if not cases:
            msg = f"선택한 케이스가 없다: {sorted(selected)}"
            raise SystemExit(msg)
    if not cases:
        msg = f"{GOLDEN_DIR}: 골든셋 케이스가 없다"
        raise SystemExit(msg)
    return cases


def expected_keys(case: dict[str, Any]) -> set[tuple[str, int]]:
    """정답 문단 `(doc_id, 조 번호)` 집합."""
    out: set[tuple[str, int]] = set()
    for entry in case.get("expected_impacts") or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        out.add((str(entry["doc_id"]), number))
    return out


async def run(model: str, selected: set[str] | None) -> None:
    cases = load_cases(selected)
    llm = ClaudeClient(model)
    embedding = build_embedding(EMBEDDING)
    store = LocalDocumentStore(snapshot_root())

    rows: list[dict[str, Any]] = []
    totals = Counter[str]()
    discard_reasons = Counter[str]()

    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        for case in cases:
            source = case["source"]
            before = source.get("before")
            article = AmendedArticle(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                revision_kind=str(source.get("revision_kind") or ""),
                change_type=str(source.get("change_type") or ""),
                after_text=str(source["after"]),
                # `TODO(verify)` 는 확보하지 못했다는 표시다. 그대로 넘기면 모델이
                # 그것을 개정 전 문구로 읽는다 (골든셋 README §5).
                before_text=(None if not before or "TODO(verify)" in str(before) else str(before)),
                document_versions={"law_mst": str(source.get("mst", ""))},
            )

            outcome = await extract_obligations(
                conn,
                switches=switches,
                article=article,
                llm=llm,
                embedding=embedding,
                store=store,
                as_of=AS_OF,
                mode=SearchMode.HYBRID,
            )

            gate = outcome.gate
            cited = {
                (chunk.doc_id, chunk.article_no)
                for chunk in outcome.retrieval.chunks
                if str(chunk.paragraph_id)
                in {c.paragraph_id for o in gate.supported for c in o.citations}
            }
            expected = expected_keys(case)
            for reason in (d.reason.value for d in gate.discarded):
                discard_reasons[reason] += 1

            totals["input_tokens"] += _tokens(outcome, "input")
            totals["output_tokens"] += _tokens(outcome, "output")
            totals["cache_read_tokens"] += _tokens(outcome, "cache_read")
            totals["cache_creation_tokens"] += _tokens(outcome, "cache_creation")
            totals["attempts"] += outcome.attempts

            row = {
                "case_id": case["id"],
                "expected_outcome": case["expected_outcome"],
                "gate_status": gate.status.value,
                "obligations_supported": len(gate.supported),
                "obligations_unsupported": len(gate.unsupported),
                "obligations_removed": len(gate.removed),
                "citations_kept": gate.citation_count,
                "citations_discarded": len(gate.discarded),
                "cited": sorted(f"{d}#{n}" for d, n in cited),
                "expected": sorted(f"{d}#{n}" for d, n in expected),
                "hit": sorted(f"{d}#{n}" for d, n in (cited & expected)),
                "suggested_action": gate.suggested_action.value,
                "attempts": outcome.attempts,
                "injection_signals": list(outcome.injection_signals),
                "invocation_ids": [str(i) for i in outcome.invocation_ids],
            }
            rows.append(row)
            logger.info(
                "%s: %s  근거 %d건 / 폐기 %d건 / 정답적중 %d",
                case["id"],
                gate.status.value,
                gate.citation_count,
                len(gate.discarded),
                len(row["hit"]),
            )

    summary = _summarize(rows, totals, discard_reasons, model)
    _print(summary, discard_reasons)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"obligations-{model}-{stamp}.json"
    out.write_text(
        json.dumps(
            {"model": model, "as_of": AS_OF.isoformat(), "summary": summary, "cases": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("결과: %s", out)


def _tokens(outcome: Any, kind: str) -> int:
    """토큰 합계. 단일 진실은 `llm_invocation` 이며 이 값은 그 사본이다."""
    return int(getattr(outcome, f"{kind}_tokens_total", 0) or 0)


def _summarize(
    rows: list[dict[str, Any]],
    totals: Counter[str],
    discards: Counter[str],
    model: str,
) -> dict[str, Any]:
    """완료 조건에 대응하는 지표만 낸다. 새 지표를 여기서 만들지 않는다."""
    empty = [r for r in rows if r["expected_outcome"] == "INSUFFICIENT_EVIDENCE"]
    no_impact = [r for r in rows if r["expected_outcome"] == "NO_IMPACT"]
    impact = [r for r in rows if r["expected_outcome"] == "IMPACT"]

    zero = {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    price = PRICE_PER_MTOK.get(model, zero)
    cost = (
        totals["input_tokens"] * price["in"]
        + totals["output_tokens"] * price["out"]
        + totals["cache_read_tokens"] * price["cache_read"]
        + totals["cache_creation_tokens"] * price["cache_write"]
    ) / 1_000_000

    return {
        "cases": len(rows),
        "empty_correct": sum(
            1 for r in empty if r["gate_status"] == GateStatus.INSUFFICIENT_EVIDENCE.value
        ),
        "empty_total": len(empty),
        "no_impact_without_claims": sum(1 for r in no_impact if r["citations_kept"] == 0),
        "no_impact_total": len(no_impact),
        "impact_with_evidence": sum(1 for r in impact if r["gate_status"] == "OK"),
        "impact_total": len(impact),
        "impact_hit_any": sum(1 for r in impact if r["hit"]),
        "citations_kept": sum(r["citations_kept"] for r in rows),
        "citations_discarded": sum(r["citations_discarded"] for r in rows),
        "discard_reasons": dict(discards),
        "retries": sum(r["attempts"] - 1 for r in rows),
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cache_creation_tokens": totals["cache_creation_tokens"],
        "estimated_cost_usd": round(cost, 4),
        "price_note": "2026-08-20 공식 문서 확인. Sonnet 5 $2/$10 이 정가 (인상 취소됨)",
    }


def _print(summary: dict[str, Any], discards: Counter[str]) -> None:
    logger.info("─── 요약 ───")
    logger.info(
        "EMPTY %d/%d 가 INSUFFICIENT_EVIDENCE | NO_IMPACT 무근거주장 없음 %d/%d",
        summary["empty_correct"],
        summary["empty_total"],
        summary["no_impact_without_claims"],
        summary["no_impact_total"],
    )
    logger.info(
        "IMPACT 근거 확보 %d/%d, 정답 문단 적중 %d건",
        summary["impact_with_evidence"],
        summary["impact_total"],
        summary["impact_hit_any"],
    )
    logger.info(
        "인용 통과 %d / 폐기 %d %s | 재시도 %d회",
        summary["citations_kept"],
        summary["citations_discarded"],
        dict(discards) or "",
        summary["retries"],
    )
    logger.info(
        "토큰 in=%d out=%d → 추정 비용 $%.4f",
        summary["input_tokens"],
        summary["output_tokens"],
        summary["estimated_cost_usd"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()
    parser = argparse.ArgumentParser(description="의무사항 추출과 근거 강제 gate 평가")
    parser.add_argument(
        "--model",
        choices=["sonnet", "opus"],
        default="sonnet",
        help="기본은 sonnet. 낮게 나온 케이스만 opus 로 대조한다",
    )
    parser.add_argument(
        "--cases", default="", help="쉼표로 구분한 케이스 id (예: case-005,case-010)"
    )
    args = parser.parse_args()

    model = DEFAULT_MODEL if args.model == "sonnet" else COMPARISON_MODEL
    selected = {c.strip() for c in args.cases.split(",") if c.strip()} or None
    asyncio.run(run(model, selected))


if __name__ == "__main__":
    main()
