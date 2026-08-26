"""영향평가 + gate 3단을 골든셋 15건으로 측정한다 — 3단계 기준선과 같은 규약으로.

    uv run --group eval python -m evals.runners.impact_eval --model sonnet
    uv run --group eval python -m evals.runners.impact_eval --no-promote --cases case-013

무엇을 재는가 (4단계 지시 §7):

1. **부서 배정 일치율** — 골든셋 `expected_departments` 와 대조한다.
2. **gate 3단 판정 분포** — SUPPORTED / PARTIAL / UNSUPPORTED.
3. **재작성 발생률** — ADR-013 의 「틀렸음을 알게 되는 신호 2번」.
4. **case-013 을 gate 3단이 잡는가** — 이 게이트를 만든 이유가 그 케이스다.
5. **R-22 3항목이 위임 승격으로 회수되는가.**
6. **비용** — 3단계의 $0.7675(15건)와 비교한다. 노드가 늘었으므로 늘어난다.

**검색 규약을 고치지 않는다** (`docs/10-retrieval-evaluation-protocol.md`). k=10, HYBRID,
`as_of=2026-02-01`, KURE-v1 그대로다. 승격은 검색 파라미터가 아니라 후보 추가이므로
`--no-promote` 로 껐다 켰다 하며 **같은 규약 안에서** 비교한다.
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
from regchange.adapters.llm import LLMError
from regchange.adapters.llm.claude import COMPARISON_MODEL, DEFAULT_MODEL, ClaudeClient
from regchange.adapters.storage.local import LocalDocumentStore
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv, snapshot_root
from regchange.guards.killswitch import SwitchGate
from regchange.pipeline.impact import GroundingMode, assess_impact, build_context
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval import build_query, parse_article_spec
from regchange.retrieval.models import RetrievalSource, SearchMode
from regchange.retrieval.promote import DEFAULT_TOP_N, promote_by_delegation
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole, role_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"
RESULTS_DIR = REPO_ROOT / "evals" / "results"

AS_OF = dt.date(2026, 2, 1)
EMBEDDING = "kure-v1"
TOP_K = 10

ABORT_AFTER_CONSECUTIVE_ERRORS = 2
"""호출 실패가 이만큼 연달아 나면 실행을 중단한다.

**2026-08-22 사건에서 나온 값이다.** 크레딧이 소진된 상태로 15케이스를 끝까지 돌았고,
결과 파일은 토큰 0·비용 $0 인데 요약은 「EMPTY 이관 3/3」으로 정상처럼 보였다.
1 로 두지 않는 이유는 일시적 네트워크 오류 한 번으로 측정을 버리지 않기 위해서다 —
연달아 두 번이면 그것은 일시적이지 않다."""

R22_ITEMS = {("ISP-POL-001", 5), ("ISP-POL-001", 8), ("ISP-POL-001", 15)}
"""1차 검색이 13·82·72위로 밀어낸 정책 계층 조항 3항목 (R-22)."""

PRICE_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5": {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}
"""3단계 러너와 같은 상수. 두 곳에 두는 것은 중복이지만, 한쪽을 고치면 두 측정의 비용이
같은 기준으로 비교되지 않는다는 사실이 드러나야 한다."""

logger = logging.getLogger("impact_eval")


def _refs(entries: list[dict[str, Any]] | None) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for entry in entries or []:
        number, _ = parse_article_spec(str(entry["article_spec"]))
        out.add((str(entry["doc_id"]), number))
    return out


def load_cases(selected: set[str] | None) -> list[dict[str, Any]]:
    cases = [
        yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(GOLDEN_DIR.glob("case-*.yaml"))
    ]
    if selected:
        cases = [c for c in cases if c["id"] in selected]
    if not cases:
        msg = f"{GOLDEN_DIR}: 대상 케이스가 없다"
        raise SystemExit(msg)
    return cases


async def run(model: str, selected: set[str] | None, *, promote: bool, mode: GroundingMode) -> None:
    cases = load_cases(selected)
    llm = ClaudeClient(model)
    embedding = build_embedding(EMBEDDING)
    store = LocalDocumentStore(snapshot_root())

    rows: list[dict[str, Any]] = []
    call_errors: list[dict[str, str]] = []
    consecutive_errors = 0
    aborted = False
    totals = Counter[str]()
    levels = Counter[str]()
    relations = Counter[str]()

    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        for case in cases:
            source = case["source"]
            before = source.get("before")
            query = build_query(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                after_text=str(source["after"]),
            )
            retrieval = await search(
                conn,
                switches=switches,
                query=query,
                mode=SearchMode.HYBRID,
                limit=TOP_K,
                as_of=AS_OF,
                client=embedding,
            )
            if promote:
                retrieval = await promote_by_delegation(
                    conn,
                    switches=switches,
                    result=retrieval,
                    query=query,
                    as_of=AS_OF,
                    top_n=DEFAULT_TOP_N,
                    client=embedding,
                    mode=SearchMode.HYBRID,
                )

            article = AmendedArticle(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                revision_kind=str(source.get("revision_kind") or ""),
                change_type=str(source.get("change_type") or ""),
                after_text=str(source["after"]),
                before_text=(None if not before or "TODO(verify)" in str(before) else str(before)),
                document_versions={
                    "law_mst": str(source.get("mst", "")),
                    "effective_date": str(source.get("effective_date", "")),
                },
            )
            try:
                obligations = await extract_obligations(
                    conn,
                    switches=switches,
                    article=article,
                    llm=llm,
                    embedding=embedding,
                    store=store,
                    as_of=AS_OF,
                    retrieval=retrieval,
                )

                ctx = build_context(
                    law_name=article.law_name,
                    article_path=article.article_path,
                    revision_kind=article.revision_kind,
                    change_type=article.change_type,
                    after_text=article.after_text,
                    obligations=obligations.gate,
                    retrieval=retrieval,
                    document_versions=article.document_versions or {},
                )
                impact = await assess_impact(
                    conn, switches=switches, ctx=ctx, llm=llm, store=store, mode=mode
                )
            except LLMError as exc:
                # **실패를 결과로 위장하지 않는다** (2026-08-22 사건). 이 케이스는 채점
                # 대상에서 빠지고, 요약이 `valid=False` 가 되며, 연달아 실패하면 멈춘다.
                call_errors.append({"case_id": str(case["id"]), "error": str(exc)})
                consecutive_errors += 1
                logger.exception(
                    "%s: 호출 실패 — 채점에서 제외한다 (연속 %d회)",
                    case["id"],
                    consecutive_errors,
                )
                if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
                    logger.exception(
                        "호출 실패가 %d회 연속이다. 실행을 중단한다 — 크레딧·자격증명·"
                        "네트워크를 확인하라. 남은 %d케이스는 돌지 않았다",
                        consecutive_errors,
                        len(cases) - len(rows) - len(call_errors),
                    )
                    aborted = True
                    break
                continue
            consecutive_errors = 0

            by_id = {str(c.paragraph_id): c for c in retrieval.chunks}
            cited = {
                by_id[i.paragraph_id].key for i in impact.draft.impacts if i.paragraph_id in by_id
            }
            cited_promoted = {
                by_id[i.paragraph_id].key
                for i in impact.draft.impacts
                if i.paragraph_id in by_id
                and by_id[i.paragraph_id].source is RetrievalSource.DELEGATION_PROMOTED
            }
            expected = _refs(case.get("expected_impacts"))
            decoys = _refs(case.get("decoys"))
            expected_depts = {str(d) for d in case.get("expected_departments") or []}
            got_depts = set(impact.draft.affected_departments)

            for level, count in impact.grounding.counts.items():
                levels[level.value] += count
            for relation, count in impact.grounding.relation_counts.items():
                relations[relation.value] += count

            totals["input_tokens"] += obligations.input_tokens_total + impact.input_tokens_total
            totals["output_tokens"] += obligations.output_tokens_total + impact.output_tokens_total
            # **캐시 토큰은 `input_tokens` 에 들어 있지 않다.** API 가 브레이크포인트
            # 이후의 토큰만 `input_tokens` 로 준다 — 더하지 않으면 캐싱 도입 이후의
            # 비용이 조용히 작게 나오고, 캐싱 전후 실행을 나란히 놓을 수 없게 된다.
            totals["cache_read_tokens"] += (
                obligations.cache_read_tokens_total + impact.cache_read_tokens_total
            )
            totals["cache_write_tokens"] += (
                obligations.cache_creation_tokens_total + impact.cache_creation_tokens_total
            )
            totals["revisions"] += impact.revisions
            totals["cases_with_rewrite"] += 1 if impact.revisions else 0
            totals["blind_calls"] += impact.blind_calls
            totals["blind_cache_hits"] += impact.blind_cache_hits

            rows.append(
                {
                    "case_id": case["id"],
                    "expected_outcome": case["expected_outcome"],
                    # 유형과 B-1 실측을 결과에 실어 보낸다. **집계기가 케이스 파일을
                    # 다시 읽지 않게 하기 위해서다** — 결과 파일과 케이스 파일이 갈리면
                    # 어느 쪽 기준으로 집계했는지 나중에 알 수 없다.
                    "case_type": case.get("case_type"),
                    "b1_matched": case.get("b1_matched"),
                    "b1_match_kind": case.get("b1_match_kind"),
                    "obligation_status": obligations.gate.status.value,
                    "impact_status": impact.status.value,
                    "risk_level": impact.draft.risk_level.value,
                    "expected_risk": case.get("expected_risk"),
                    "confidence": impact.draft.confidence.value,
                    "impacts": len(impact.draft.impacts),
                    "cited": sorted(f"{d}#{n}" for d, n in cited),
                    "expected": sorted(f"{d}#{n}" for d, n in expected),
                    "hit": sorted(f"{d}#{n}" for d, n in (cited & expected)),
                    "decoy_cited": sorted(f"{d}#{n}" for d, n in (cited & decoys)),
                    "cited_promoted": sorted(f"{d}#{n}" for d, n in cited_promoted),
                    "r22_cited": sorted(f"{d}#{n}" for d, n in (cited & R22_ITEMS)),
                    "departments": sorted(got_depts),
                    "expected_departments": sorted(expected_depts),
                    "dept_hit": sorted(got_depts & expected_depts),
                    "dept_extra": sorted(got_depts - expected_depts),
                    "dept_basis": [
                        {
                            "department": d.department,
                            "derivation": d.derivation.value,
                            "basis_is_affected": impact.draft.basis_is_affected(d),
                            "quote": d.basis_quote[:60],
                        }
                        for d in impact.draft.departments
                    ],
                    "grounding": {
                        level.value: count for level, count in impact.grounding.counts.items()
                    },
                    "relations": {r.value: n for r, n in impact.grounding.relation_counts.items()},
                    "judgments": [
                        {
                            "key": j.key,
                            "level": j.level.value,
                            "relation": j.relation.value if j.relation else None,
                            "reason": j.reason,
                        }
                        for j in impact.grounding.judgments
                    ],
                    "unsupported": list(impact.grounding.unsupported_keys),
                    "unsupported_ratio": impact.grounding.unsupported_ratio,
                    "revisions": impact.revisions,
                    "consistency_violations": [v.rule.value for v in impact.consistency.violations],
                    "draft_discarded": [d.reason.value for d in impact.gate.discarded],
                    "verification_error": impact.verification_error,
                    "assessment_id": str(impact.assessment_id),
                    "control_items": list(impact.draft.control_items),
                    "required_evidence": list(impact.draft.required_evidence),
                }
            )
            logger.info(
                "%s: %s 영향 %d건 부서 %s (판정 %s, 재작성 %d)",
                case["id"],
                impact.status.value,
                len(impact.draft.impacts),
                sorted(got_depts) or "-",
                rows[-1]["grounding"],
                impact.revisions,
            )

    summary = _summarize(rows, totals, levels, relations, model, promote=promote, mode=mode)
    summary["call_errors"] = call_errors
    summary["cases_measured"] = len(rows)
    summary["cases_total"] = len(cases)
    summary["aborted"] = aborted
    # **이 한 줄이 사건의 재발을 막는다.** 호출이 한 번이라도 실패했으면 이 실행의
    # 수치는 다른 실행과 비교할 수 없다. 집계기(`variability.py`)가 이 값을 본다.
    summary["valid"] = not call_errors and not aborted and len(rows) == len(cases)
    _print(summary)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = ("" if promote else "-nopromote") + (
        "-deanchored" if mode is GroundingMode.DE_ANCHORED else ""
    )
    out = RESULTS_DIR / f"impact-{model}{suffix}-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "model": model,
                "as_of": AS_OF.isoformat(),
                "promote": promote,
                "grounding_mode": mode.value,
                "summary": summary,
                "cases": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("결과: %s", out)


MIN_TYPE_CASES_FOR_RATIO = 3
"""유형별 정확도에 **비율을 적어도 되는** 최소 건수.

근거는 통계가 아니라 오독 방지다. n=1 이면 결과가 1.0000 또는 0.0000 이고 둘 다 아무것도
뜻하지 않는다. n=2 는 한 건이 뒤집힐 때 0.5 가 움직인다. 3 은 "적다"고 말할 수는 있어도
**한 건이 전체를 뒤집지는 않는** 최소 지점이다.

42건 구성에서 이 문턱에 걸리는 것은 `DECOY_ONLY`(1건)와 `EMPTY_NEW_PROVISION`(2건)이며,
그 사실은 `evals/datasets/golden/README.md` §3.2 에 미리 적혀 있다 —
**측정 뒤에 정한 문턱이 아니다.**"""


def _by_type(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """케이스 유형 7종별로 맞음/틀림을 센다.

    목적:
        "어느 성격의 입력에서 틀리는가"를 낸다. 총계 정확도는 그 질문에 답하지 못한다 —
        42건 중 22건이 「영향 없음」이므로, 아무것도 찾지 않는 시스템도 총계에서는
        절반을 맞힌다.

    구현 이유:
        **정답을 `expected_outcome` 이 아니라 시스템이 낼 수 있는 값으로 환산한다.**
        `AssessmentStatus` 에는 `NO_IMPACT` 가 없다 — 시스템은 "없다"고 말하지 않고
        "모른다"(`INSUFFICIENT_EVIDENCE`)고 말한다. 따라서 `NO_IMPACT` 케이스와
        `INSUFFICIENT_EVIDENCE` 케이스의 **정답 출력이 같다.** 이 환산을 하지 않으면
        타법개정 7건이 전부 오답으로 세어진다.

        그 결과 유형 7종 중 6종의 정답 출력이 같아지고, 이 축이 재는 것은
        **"어떤 종류의 영향 없음에서 시스템이 잘못 찾아내는가"** 가 된다.
        그것이 `docs/16-baseline-comparison.md` 가 지목한 우리 기여(「고르기」와
        「모른다고 말하기」)의 시험이다.

    트레이드오프:
        `IMPACT` 유형에서 `OK` 와 `NEEDS_REVIEW` 를 함께 「찾음」으로 센다. 둘을 가르면
        칸이 두 배가 되고 유형당 건수가 더 줄어 판정 불가가 늘어난다. 대신 `hit`(정답
        문단을 실제로 인용했는가)를 따로 실어 **찾았지만 틀린 곳을 찾은 경우**를 구별한다.

        비율을 항상 내지 않는다. `MIN_TYPE_CASES_FOR_RATIO` 미만이면 `ratio` 가 None 이고
        `verdict` 가 `TOO_FEW` 다 — **0.0 으로 채우면 "못 맞혔다"와 "셀 수 없다"가 같은
        값이 된다.**

    엣지 케이스:
        - 유형이 결과에 없음(`case_type` 이 None): `UNTYPED` 칸에 모은다. 조용히 버리면
          합이 총계와 어긋나고, 그 어긋남은 표에서 보이지 않는다
        - 표본 실행처럼 일부 유형이 아예 없음: 그 유형의 칸을 만들지 않는다.
          0/0 을 만들면 "돌리지 않았다"와 "돌렸는데 0건"이 같아진다
        - 호출 실패로 제외된 케이스: 애초에 `rows` 에 없다. 러너가 `call_errors` 로
          따로 세고 요약의 `valid` 를 내린다
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("case_type") or "UNTYPED"), []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for name, group in sorted(buckets.items()):
        wants_impact = name == "IMPACT"
        wrong = [
            r for r in group if (r["impact_status"] != "INSUFFICIENT_EVIDENCE") is not wants_impact
        ]
        correct = len(group) - len(wrong)
        enough = len(group) >= MIN_TYPE_CASES_FOR_RATIO
        out[name] = {
            "n": len(group),
            "expected_status": (
                "not INSUFFICIENT_EVIDENCE" if wants_impact else "INSUFFICIENT_EVIDENCE"
            ),
            "correct": correct,
            "ratio": round(correct / len(group), 4) if enough else None,
            "verdict": "OK" if enough else "TOO_FEW",
            "hit": sum(1 for r in group if r["hit"]) if wants_impact else None,
            "decoy_cited": sum(len(r["decoy_cited"]) for r in group),
            "cases_wrong": sorted(r["case_id"] for r in wrong),
        }
    return out


def _summarize(
    rows: list[dict[str, Any]],
    totals: Counter[str],
    levels: Counter[str],
    relations: Counter[str],
    model: str,
    *,
    promote: bool,
    mode: GroundingMode,
) -> dict[str, Any]:
    """완료 조건에 대응하는 지표만 낸다. 새 지표를 여기서 만들지 않는다."""
    zero = {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    price = PRICE_PER_MTOK.get(model, zero)
    # 네 단가를 모두 쓴다. **캐시 도입 전에는 뒤의 두 항이 0이었을 뿐 식은 같다** —
    # 캐싱 전 실행과 캐싱 후 실행의 비용이 같은 식으로 계산돼야 비교가 성립한다.
    cost = (
        totals["input_tokens"] * price["in"]
        + totals["output_tokens"] * price["out"]
        + totals["cache_read_tokens"] * price["cache_read"]
        + totals["cache_write_tokens"] * price["cache_write"]
    ) / 1_000_000
    billed_input = (
        totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_write_tokens"]
    )

    scored = [r for r in rows if r["expected_departments"]]
    dept_exact = sum(1 for r in scored if set(r["departments"]) == set(r["expected_departments"]))
    dept_any = sum(1 for r in scored if r["dept_hit"])
    dept_recall = (
        sum(len(r["dept_hit"]) / len(r["expected_departments"]) for r in scored) / len(scored)
        if scored
        else None
    )

    impact_cases = [r for r in rows if r["expected_outcome"] == "IMPACT"]
    empty_cases = [r for r in rows if r["expected_outcome"] == "INSUFFICIENT_EVIDENCE"]
    case013 = next((r for r in rows if r["case_id"] == "case-013"), None)

    return {
        "cases": len(rows),
        "promote": promote,
        # 유형별 정확도 7종. **총계 정확도가 답하지 못하는 질문에 답한다** —
        # 42건 중 22건이 「영향 없음」이라 아무것도 찾지 않는 시스템도 절반을 맞힌다.
        "by_type": _by_type(rows),
        "grounding_mode": mode.value,
        "relations": dict(relations),
        # 블라인드 호출 수와 캐시 히트. de-anchored 에서만 0이 아니다 — 호출이 얼마나
        # 늘었는지를 이 둘 없이는 말할 수 없다.
        "blind_calls": totals["blind_calls"],
        "blind_cache_hits": totals["blind_cache_hits"],
        # 호출 실패로 이관된 건. **측정이 조용히 나빠지지 않게** 따로 센다 — 네트워크
        # 오류 한 번이 케이스를 INSUFFICIENT_EVIDENCE 로 만들고, 그것은 "근거가 없어서
        # 이관"과 다른 사실이다.
        "verification_errors": [r["case_id"] for r in rows if r["verification_error"]],
        "impact_with_evidence": sum(
            1 for r in impact_cases if r["impact_status"] != "INSUFFICIENT_EVIDENCE"
        ),
        "impact_total": len(impact_cases),
        "impact_hit_any": sum(1 for r in impact_cases if r["hit"]),
        "empty_correct": sum(
            1 for r in empty_cases if r["impact_status"] == "INSUFFICIENT_EVIDENCE"
        ),
        "empty_total": len(empty_cases),
        "dept_scored": len(scored),
        "dept_exact": dept_exact,
        "dept_any": dept_any,
        "dept_recall": dept_recall,
        "grounding": dict(levels),
        "unsupported_total": levels.get("UNSUPPORTED", 0),
        "rewrite_cases": totals["cases_with_rewrite"],
        "rewrite_rate": totals["cases_with_rewrite"] / len(rows) if rows else 0.0,
        "risk_exact": sum(1 for r in rows if r["risk_level"] == r["expected_risk"]),
        "decoy_cited": sum(len(r["decoy_cited"]) for r in rows),
        "promoted_cited": sum(len(r["cited_promoted"]) for r in rows),
        "r22_cited": sorted({v for r in rows for v in r["r22_cited"]}),
        "case013": None
        if case013 is None
        else {
            "status": case013["impact_status"],
            "impacts": case013["impacts"],
            "grounding": case013["grounding"],
            "revisions": case013["revisions"],
            "caught": case013["impact_status"] == "INSUFFICIENT_EVIDENCE",
        },
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cache_write_tokens": totals["cache_write_tokens"],
        # 청구 대상 입력 토큰 전체(비캐시 + 읽기 + 쓰기). 캐싱 전 실행의
        # `input_tokens` 와 이 값을 비교해야 "같은 일을 얼마에 했는가"가 나온다.
        "billed_input_tokens": billed_input,
        # 캐시가 실제로 들었는가. 분모가 0이면 None 이다 — 0.0 으로 채우면
        # "캐시를 안 썼다"와 "썼는데 하나도 안 맞았다"가 같은 값이 된다.
        "cache_hit_ratio": (
            round(totals["cache_read_tokens"] / billed_input, 4) if billed_input else None
        ),
        "estimated_cost_usd": round(cost, 4),
        "baseline_cost_usd": 0.7675,
        "price_note": "2026-08-20 공식 문서 확인. Sonnet 5 $2/$10",
    }


def _print(summary: dict[str, Any]) -> None:
    logger.info("─── 요약 ───")
    if not summary["valid"]:
        logger.error(
            "**이 실행은 무효다.** 호출 실패 %d건%s, 채점된 케이스 %d/%d. "
            "아래 수치를 다른 실행과 비교하지 마라",
            len(summary["call_errors"]),
            " (중단됨)" if summary["aborted"] else "",
            summary["cases_measured"],
            summary["cases_total"],
        )
    logger.info(
        "IMPACT 근거 확보 %d/%d (정답 적중 %d) | EMPTY 이관 %d/%d",
        summary["impact_with_evidence"],
        summary["impact_total"],
        summary["impact_hit_any"],
        summary["empty_correct"],
        summary["empty_total"],
    )
    logger.info(
        "부서: 완전일치 %d/%d, 부분일치 %d/%d, 재현율 %s",
        summary["dept_exact"],
        summary["dept_scored"],
        summary["dept_any"],
        summary["dept_scored"],
        f"{summary['dept_recall']:.4f}" if summary["dept_recall"] is not None else "—",
    )
    logger.info("gate 3단 판정: %s", summary["grounding"])
    if summary["relations"] and sum(summary["relations"].values()):
        logger.info(
            "관계 판정: %s | 블라인드 호출 %d (캐시 히트 %d)",
            summary["relations"],
            summary["blind_calls"],
            summary["blind_cache_hits"],
        )
    logger.info(
        "재작성 %d건 / %d케이스 (%.1f%%)",
        summary["rewrite_cases"],
        summary["cases"],
        summary["rewrite_rate"] * 100,
    )
    logger.info(
        "위험도 일치 %d/%d | decoy 인용 %d | 승격 문단 인용 %d | R-22 인용 %s",
        summary["risk_exact"],
        summary["cases"],
        summary["decoy_cited"],
        summary["promoted_cited"],
        summary["r22_cited"] or "없음",
    )
    logger.info("case-013: %s", summary["case013"])
    if summary["verification_errors"]:
        logger.warning(
            "**검증 호출이 실패해 이관된 케이스: %s** — 이 값이 0이 아니면 측정이 오염됐다",
            summary["verification_errors"],
        )
    logger.info(
        "토큰 in=%d (캐시읽기 %d / 쓰기 %d, 청구입력 %d, 히트율 %s) out=%d → $%.4f "
        "(3단계 기준선 $%.4f)",
        summary["input_tokens"],
        summary["cache_read_tokens"],
        summary["cache_write_tokens"],
        summary["billed_input_tokens"],
        f"{summary['cache_hit_ratio']:.4f}" if summary["cache_hit_ratio"] is not None else "—",
        summary["output_tokens"],
        summary["estimated_cost_usd"],
        summary["baseline_cost_usd"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()
    parser = argparse.ArgumentParser(description="영향평가 + gate 3단 평가")
    parser.add_argument("--model", choices=["sonnet", "opus"], default="sonnet")
    parser.add_argument("--cases", default="")
    parser.add_argument(
        "--grounding",
        choices=["anchored", "de-anchored"],
        default="anchored",
        help="gate 3단 검증기. 기본은 anchored(4단계 원래 구현) — 기준선을 유지한다",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="위임 승격을 끄고 돈다. 승격의 효과를 같은 규약 안에서 비교하기 위한 것이다",
    )
    args = parser.parse_args()
    model = DEFAULT_MODEL if args.model == "sonnet" else COMPARISON_MODEL
    selected = {c.strip() for c in args.cases.split(",") if c.strip()} or None
    mode = GroundingMode.DE_ANCHORED if args.grounding == "de-anchored" else GroundingMode.ANCHORED
    asyncio.run(run(model, selected, promote=not args.no_promote, mode=mode))


if __name__ == "__main__":
    main()
