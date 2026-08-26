"""라우팅 사전 확인 — 경량 경로 C 가 42건 실측에서 성립하는가. LLM 을 부르지 않는다.

    uv run --group eval python -m evals.runners.routing_precheck
    uv run --group eval python -m evals.runners.routing_precheck --result <경로>

목적:
    `제개정구분명 == 타법개정` 을 경량 경로로 보내는 규칙 라우팅을 **구현하기 전에**,
    그 경량 경로의 판정 기준("검색만 하고 결과가 비면 이관")이 42건 실측에서 실제로
    성립하는지를 데이터로 확인한다. 확인 대상은 셋이다.

    1. **분류가 갈리는가** — `제개정구분명` 이 케이스 유형과 어긋나는 건이 있는가
    2. **검색 결과가 비는가** — 타법개정 케이스에서 검색이 0건을 내는가
    3. **점수가 갈리는가** — 0건이 아니라면 점수로 가를 수 있는가
    그리고 라우팅으로 얻을 수 있는 **절감의 상한**을 기록에서 계산한다.

구현 이유:
    **LLM 을 다시 부르지 않는다.** 여기서 답해야 하는 질문은 전부 검색과 호출 기록에
    대한 것이고, 모델을 다시 부르면 R-27(케이스 단위 변동)이 답을 흔든다.
    `llm_invocation.retrieved_chunk_ids` 가 그때 검색된 문단 집합을 남기므로(ADR-013)
    「무엇을 가져왔는가」는 기록으로 답한다.

    **점수는 기록에 없어서 검색을 다시 돌린다.** `llm_invocation` 은 문단 ID 만 남기고
    점수는 남기지 않는다. 검색은 결정론적이므로(`docs/15-variability-results.md` —
    3회 완전 일치, 편차 0) 다시 돌린 점수를 그때의 점수로 쓸 수 있지만, **그 전제를
    가정하지 않고 검사한다** — 재실행 결과의 문단 ID 집합을 기록과 대조해
    `REPRODUCED`/`DRIFT` 로 표시하고, `DRIFT` 인 케이스의 점수는 판정에서 뺀다.

    **세 척도를 함께 낸다.** 운영 경로는 HYBRID 이고 그 점수는 RRF 다. RRF 는 순위만
    보므로(`retrieval/fusion.py`) 그 점수에는 「질의와 얼마나 닮았는가」가 들어 있지
    않다 — 임계를 걸 대상이 아니다. 그래서 임계의 재료가 될 수 있는 원점수(VECTOR 의
    코사인, LEXICAL 의 BM25)를 같은 `search()` 로 함께 재고, **어느 척도로도 갈리지
    않는다**는 것까지 확인해야 「임계를 만들지 않는다」가 근거 있는 결론이 된다.

트레이드오프:
    VECTOR·LEXICAL 를 추가로 돌리는 것은 운영 경로가 아니다. 진단 목적이며 그 점수는
    운영 판정에 쓰이지 않는다 — 쓰려면 그것 자체가 검색 파라미터 변경이고 별도 근거가
    필요하다. 이 러너는 「그 근거가 있는가」를 확인할 뿐 만들지 않는다.

    비용 귀속에서 **의무사항 추출 호출은 시각으로 귀속한다.** 그 행에는
    `impact_assessment_id` 가 없다(추출이 평가보다 먼저 일어나므로 아직 ID 가 없다).
    러너가 케이스를 순차 처리하므로 「직전의 추출 호출」이 그 케이스의 것이지만,
    그것을 가정하지 않고 **앞선 영향평가 행이 사이에 끼어 있으면 `NO_RECORD`** 로
    두어 조용한 오귀속을 만들지 않는다. 귀속 합계와 결과 파일의 실측 비용을 나란히
    내어 빠진 것이 있으면 드러나게 한다.

엣지 케이스:
    - 기록에 없는 평가 ID: `NO_RECORD`. 0 으로 채우지 않는다
    - 재실행이 기록과 다른 문단을 냄: `DRIFT` 로 표시하고 그 케이스의 점수를 판정
      분모에서 뺀다. 검색이 결정론적이라는 전제가 깨진 것이므로 조용히 넘기지 않는다
    - 승격(`DELEGATION_PROMOTED`) 문단: 재현 대조에는 포함하고(기록에 들어 있다)
      **점수 분포에서는 제외한다.** 승격은 좁힌 범위에서 다시 검색한 결과라 1차 검색
      점수와 비교 가능하지 않다 (`retrieval/search.py` 의 `doc_ids` 주석)
    - DB 가 없음: 즉시 실패한다. 이 러너의 모든 질문이 DB 를 필요로 한다

판정 규칙 — **측정 전에 고정한다.**

    R-1 「0건 기준」: 경량 경로 후보(타법개정) 전건에서 검색 결과가 0건이어야 C 가
        그대로 성립한다. 결과가 있는 케이스는 기존 경로로 떨어지므로 그만큼 절감이
        사라진다. **11/11 이 아니면 「0건 기준으로 C 는 통하지 않는다」로 적는다.**

    R-2 「점수 임계」: 어떤 척도에서든
        `max(타법개정 1위 점수) < min(IMPACT 1위 점수)` 이면 그 사이에 임계를 둘 수
        있다. 한 건이라도 겹치면 **임계를 만들지 않는다** (CLAUDE.md §4 — 근거 없는
        상수 금지). 겹침의 크기도 함께 적는다.

    R-3 「절감 상한」: 타법개정 케이스가 42건 실측 비용에서 차지하는 비율. 경량 경로가
        LLM 을 0회 부를 때의 값이며 **이보다 큰 절감은 나오지 않는다.**
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

from evals.runners.impact_eval import AS_OF, EMBEDDING, PRICE_PER_MTOK, TOP_K
from evals.runners.impact_report import (
    RESULTS_DIR,
    latest_result,
    paragraph_map,
    retrieved_by_assessment,
)
from evals.runners.policy_index import build_client as build_embedding
from regchange.adapters.switches import PostgresSwitchStore
from regchange.config.settings import apply_dotenv
from regchange.guards.killswitch import SwitchGate
from regchange.retrieval import build_query
from regchange.retrieval.models import RetrievalSource, SearchMode
from regchange.retrieval.promote import DEFAULT_TOP_N, promote_by_delegation
from regchange.retrieval.search import search
from regchange.store.dsn import DbRole, role_dsn

logger = logging.getLogger("routing_precheck")

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "datasets" / "golden"

LIGHTWEIGHT_KIND = "타법개정"
"""경량 경로로 보낼 `제개정구분명` 값. **이것 하나만 쓴다** — 개정 규모나 조문 수 같은
다른 기준을 더하면 근거 없는 조건이 하나 더 생긴다."""

TOP_N_FOR_SCORE = 3
"""점수 분포를 볼 때 함께 내는 상위 구간. 1위만 보면 한 건의 요동에 판정이 걸리고,
10위까지 평균하면 꼬리가 차이를 지운다. **판정은 1위로 한다** — R-2 가 그렇게 적혀
있으며, 상위 3 평균은 1위가 우연인지 보기 위한 참고값이다."""

DIAGNOSTIC_MODES = (SearchMode.HYBRID, SearchMode.VECTOR, SearchMode.LEXICAL)
"""점수를 재는 세 척도. HYBRID 만 운영 경로이고 나머지 둘은 진단이다."""

OPERATIONAL_LIGHTWEIGHT_SHARE = 0.562
"""12개월 조문 이벤트 전수에서 타법개정이 차지하는 비율 (`evals/datasets/golden/README.md`
§ 유형 분포, `docs/domain-selection/amendment-frequency.md` 의 전수 측정).

**골든 42건의 비율(11/42 = 0.262)이 아니다.** 골든셋은 난이도로 구성했지 운영 분포로
구성하지 않았다. 절감 상한을 운영 분포로 환산할 때만 쓰며, 환산값에는 케이스당 비용이
운영에서도 같다는 가정이 들어간다 — 그 가정을 결과에 함께 적는다."""


def load_golden() -> dict[str, dict[str, Any]]:
    """골든 케이스를 id → 원본 딕셔너리로 읽는다."""
    return {
        c["id"]: c
        for c in (
            yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in sorted(GOLDEN_DIR.glob("case-*.yaml"))
        )
    }


def routing_group(revision_kind: str | None) -> str:
    """`제개정구분명` 하나로 경로를 정한다. 라우팅 노드가 쓸 규칙과 같은 판정이다."""
    return "LIGHTWEIGHT" if (revision_kind or "").strip() == LIGHTWEIGHT_KIND else "FULL"


def classification_crosstab(golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`제개정구분명` 대 케이스 유형 교차표와 오분류 목록.

    목적:
        규칙 라우팅이 42건에서 무엇을 어디로 보내는지, 그리고 **실질 개정을 경량 경로로
        보내는 건이 있는지**(F-1 방향의 오분류)를 센다.

    구현 이유:
        경량 경로의 위험은 비용이 아니라 **개정을 놓치는 것**이다. 그 건수를 유형별
        정확도와 분리해 따로 센다 — 정확도 표에 섞이면 「전체가 좋아졌다」에 묻힌다.

    트레이드오프:
        여기서 세는 오분류는 **골든셋의 유형 라벨 기준**이다. 그 라벨은 우리가 붙였고,
        타법개정 11건은 전부 「영향 없음」으로 설계됐다. 그래서 이 표의 오분류 0 은
        「규칙이 옳다」가 아니라 **「이 표본에서는 어긋나지 않는다」**까지만 뜻한다.

    엣지 케이스:
        - `revision_kind` 가 비어 있는 케이스: `FULL` 로 간다. 모르면 무거운 쪽이다
    """
    table: dict[str, dict[str, int]] = {}
    misrouted: list[dict[str, str]] = []
    for case_id, case in sorted(golden.items()):
        kind = str(case["source"].get("revision_kind") or "")
        group = routing_group(kind)
        case_type = str(case.get("case_type"))
        table.setdefault(kind or "(없음)", {}).setdefault(case_type, 0)
        table[kind or "(없음)"][case_type] += 1
        if group == "LIGHTWEIGHT" and str(case.get("expected_outcome")) == "IMPACT":
            misrouted.append({"case_id": case_id, "revision_kind": kind, "case_type": case_type})
    return {
        "table": table,
        "lightweight_cases": sorted(
            c
            for c, case in golden.items()
            if routing_group(case["source"].get("revision_kind")) == "LIGHTWEIGHT"
        ),
        "misrouted_impact_to_lightweight": misrouted,
    }


async def score_profile(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    query: str,
    client: Any,
) -> dict[str, Any]:
    """한 질의에 대해 세 척도의 상위 점수와 HYBRID 재현 결과를 낸다.

    목적:
        「검색이 무엇을 얼마나 잘 가져왔는가」를 척도별 숫자로 만든다.

    구현 이유:
        운영이 쓰는 `search()` 를 그대로 부른다. 순위 계산을 여기서 다시 구현하면
        측정이 운영과 다른 것을 재게 된다 (CLAUDE.md §4).

    트레이드오프:
        같은 질의로 검색을 세 번 돌린다. 152문단 규모에서 수십 밀리초이고, 세 척도를
        한 번에 얻으려면 결합 이전 순위를 꺼내야 하는데 그것은 내부 함수 호출이다.

    엣지 케이스:
        - 결과 0건: `top1` 이 `None` 이다. 0.0 으로 채우지 않는다 — 「점수가 0」과
          「점수가 없다」는 다른 사실이다
    """
    out: dict[str, Any] = {}
    for mode in DIAGNOSTIC_MODES:
        result = await search(
            conn,
            switches=switches,
            query=query,
            mode=mode,
            limit=TOP_K,
            as_of=AS_OF,
            client=client if mode is not SearchMode.LEXICAL else None,
        )
        primary = [c for c in result.chunks if c.source is RetrievalSource.PRIMARY]
        scores = [float(c.score) for c in primary]
        out[mode.value] = {
            "n": len(primary),
            "top1": scores[0] if scores else None,
            "top3_mean": (
                round(sum(scores[:TOP_N_FOR_SCORE]) / len(scores[:TOP_N_FOR_SCORE]), 6)
                if scores
                else None
            ),
            "scores": [round(s, 6) for s in scores],
        }
        if mode is SearchMode.HYBRID:
            promoted = await promote_by_delegation(
                conn,
                switches=switches,
                result=result,
                query=query,
                as_of=AS_OF,
                top_n=DEFAULT_TOP_N,
                client=client,
                mode=SearchMode.HYBRID,
            )
            out["rerun_chunk_ids"] = [str(c.paragraph_id) for c in promoted.chunks]
    return out


def attribute_costs(
    conn: psycopg.Connection[Any],
    assessment_ids: list[str],
    model: str,
) -> dict[str, dict[str, Any]]:
    """평가 ID 별 LLM 호출 수·토큰·비용을 기록에서 귀속한다.

    목적:
        경량 경로가 없앨 수 있는 비용의 상한을 케이스 단위로 계산한다.

    구현 이유:
        영향평가와 인용검증 행은 `impact_assessment_id` 로 직접 걸린다. **의무사항 추출
        행은 걸리지 않는다** — 추출이 평가보다 먼저이므로 그 시점에 평가 ID 가 없다.
        러너가 케이스를 순차 처리하므로 「직전의 추출 호출」이 그 케이스의 것이고,
        그 규칙을 명시적으로 적용하되 **앞선 평가 행이 사이에 있으면 귀속하지 않는다.**

    트레이드오프:
        시각 귀속은 실행이 순차라는 사실에 기댄다. 병렬 실행을 도입하면 이 함수는
        틀린 답을 낸다 — 그때 조용히 틀리지 않도록 「사이에 다른 평가 행이 없을 것」을
        조건으로 걸었고, 위반하면 `NO_RECORD` 가 되어 합계가 실측 비용보다 작아진다.

    엣지 케이스:
        - 실패한 호출(`outcome <> 'OK'`): 포함한다. 실패도 청구된다
        - 토큰이 NULL 인 행(네트워크 오류): 0 으로 세지 않고 `null_token_rows` 로 센다
    """
    rows = conn.execute(
        """
        select id::text,
               impact_assessment_id::text,
               purpose,
               invoked_at,
               input_tokens,
               output_tokens,
               cache_read_input_tokens,
               cache_creation_input_tokens,
               outcome,
               latency_ms
          from llm_invocation
         order by invoked_at
        """
    ).fetchall()

    price = PRICE_PER_MTOK.get(
        model, {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    )

    def cost_of(row: tuple[Any, ...]) -> tuple[float, bool]:
        _, _, _, _, tin, tout, tcr, tcw, _, _ = row
        if tin is None and tout is None:
            return 0.0, True
        usd = (
            (tin or 0) * price["in"]
            + (tout or 0) * price["out"]
            + (tcr or 0) * price["cache_read"]
            + (tcw or 0) * price["cache_write"]
        ) / 1_000_000
        return usd, False

    wanted = set(assessment_ids)
    per: dict[str, dict[str, Any]] = {
        aid: {
            "calls": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "purposes": {},
            "null_token_rows": 0,
        }
        for aid in wanted
    }

    # 1) 평가 ID 로 직접 걸리는 행
    assessment_time: dict[str, dt.datetime] = {}
    for row in rows:
        _, aid, purpose, invoked_at, *_ = row
        if aid not in wanted:
            continue
        usd, null_tokens = cost_of(row)
        entry = per[aid]
        entry["calls"] += 1
        entry["cost_usd"] += usd
        entry["latency_ms"] += int(row[9] or 0)
        entry["purposes"][purpose] = entry["purposes"].get(purpose, 0) + 1
        entry["null_token_rows"] += 1 if null_tokens else 0
        if purpose == "IMPACT_ASSESSMENT":
            assessment_time[aid] = invoked_at

    # 2) 의무사항 추출 — 직전 행으로 귀속하되 사이에 다른 평가 행이 없어야 한다
    obligations = [r for r in rows if r[2] == "OBLIGATION_EXTRACTION"]
    all_assessments = [r for r in rows if r[2] == "IMPACT_ASSESSMENT"]
    for aid, at in assessment_time.items():
        prior_assessment = max(
            (r[3] for r in all_assessments if r[3] < at),
            default=dt.datetime.min.replace(tzinfo=dt.UTC),
        )
        candidates = [r for r in obligations if prior_assessment < r[3] < at]
        if not candidates:
            per[aid]["obligation_attribution"] = "NO_RECORD"
            continue
        row = max(candidates, key=lambda r: r[3])
        usd, null_tokens = cost_of(row)
        entry = per[aid]
        entry["calls"] += 1
        entry["cost_usd"] += usd
        entry["latency_ms"] += int(row[9] or 0)
        entry["purposes"]["OBLIGATION_EXTRACTION"] = (
            entry["purposes"].get("OBLIGATION_EXTRACTION", 0) + 1
        )
        entry["null_token_rows"] += 1 if null_tokens else 0
        entry["obligation_attribution"] = "BY_TIME"

    for entry in per.values():
        entry["cost_usd"] = round(entry["cost_usd"], 6)
    return per


def separability(
    per_case: list[dict[str, Any]],
    metric: str,
) -> dict[str, Any]:
    """R-2 판정 — 경량 후보와 IMPACT 의 1위 점수가 겹치지 않는가.

    목적:
        점수 임계를 만들 근거가 있는지를 **미리 정한 규칙 하나로** 판정한다.

    구현 이유:
        `max(타법개정) < min(IMPACT)` 만 본다. 분포가 「대체로 낮다」는 것으로 임계를
        만들면 그 임계는 오분류를 내장한 채 태어나고, 그 오분류가 F-1(개정을 놓침)이다.

    트레이드오프:
        완전 분리를 요구하는 것은 보수적이다. 겹침이 한 건뿐이어도 임계를 못 만든다.
        그 대신 겹침의 크기(`overlap`)를 함께 내어 「얼마나 못 만드는가」가 보이게 했다.

    엣지 케이스:
        - 점수가 `None` 인 케이스(결과 0건): 분모에서 뺀다. 그 케이스는 R-1 이 답한다
        - `DRIFT` 케이스: 호출부가 미리 걸러서 넘긴다
    """
    light = [
        c[metric] for c in per_case if c["routing_group"] == "LIGHTWEIGHT" and c[metric] is not None
    ]
    impact = [
        c[metric]
        for c in per_case
        if c["routing_group"] == "FULL" and c["case_type"] == "IMPACT" and c[metric] is not None
    ]
    if not light or not impact:
        return {"verdict": "NOT_MEASURABLE", "light_n": len(light), "impact_n": len(impact)}
    separated = max(light) < min(impact)
    return {
        "verdict": "SEPARATED" if separated else "OVERLAP",
        "light_n": len(light),
        "impact_n": len(impact),
        "light_max": round(max(light), 6),
        "light_min": round(min(light), 6),
        "impact_max": round(max(impact), 6),
        "impact_min": round(min(impact), 6),
        "overlap": None
        if separated
        else {
            "light_above_impact_min": sum(1 for v in light if v >= min(impact)),
            "impact_below_light_max": sum(1 for v in impact if v <= max(light)),
        },
    }


async def run(result_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """§1 확인 전체를 돌려 보고서 딕셔너리를 만든다. 파일 입출력은 호출부가 한다."""
    cases = data["cases"]
    golden = load_golden()
    model = str(data["model"])

    with psycopg.connect(role_dsn(DbRole.GRAPH)) as sync_conn:
        recorded = retrieved_by_assessment(sync_conn)
        mapping = paragraph_map(sync_conn)
        costs = attribute_costs(
            sync_conn, [str(c["assessment_id"]) for c in cases if c.get("assessment_id")], model
        )

    embedding = build_embedding(EMBEDDING)
    switches = SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH)))

    per_case: list[dict[str, Any]] = []
    async with await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as conn:
        for row in cases:
            case = golden[row["case_id"]]
            source = case["source"]
            query = build_query(
                law_name=str(source["law_name"]),
                article_path=str(source.get("article_path") or ""),
                after_text=str(source["after"]),
            )
            profile = await score_profile(conn, switches=switches, query=query, client=embedding)

            aid = str(row.get("assessment_id") or "")
            recorded_ids = recorded.get(aid)
            if recorded_ids is None:
                reproduction = "NO_RECORD"
                recorded_keys: list[str] = []
            else:
                reproduction = (
                    "REPRODUCED"
                    if set(recorded_ids) == set(profile["rerun_chunk_ids"])
                    else "DRIFT"
                )
                recorded_keys = sorted(
                    f"{mapping[c][0]}#{mapping[c][1]}" for c in recorded_ids if c in mapping
                )

            cost = costs.get(aid, {})
            per_case.append(
                {
                    "case_id": row["case_id"],
                    "case_type": row.get("case_type"),
                    "revision_kind": str(source.get("revision_kind") or ""),
                    "routing_group": routing_group(source.get("revision_kind")),
                    "expected_outcome": row.get("expected_outcome"),
                    "impact_status": row.get("impact_status"),
                    "recorded_chunks": None if recorded_ids is None else len(recorded_ids),
                    "recorded_keys": recorded_keys,
                    "rerun_chunks": len(profile["rerun_chunk_ids"]),
                    "reproduction": reproduction,
                    "hybrid_top1": profile["HYBRID"]["top1"],
                    "hybrid_top3_mean": profile["HYBRID"]["top3_mean"],
                    "vector_top1": profile["VECTOR"]["top1"],
                    "vector_top3_mean": profile["VECTOR"]["top3_mean"],
                    "lexical_top1": profile["LEXICAL"]["top1"],
                    "lexical_top3_mean": profile["LEXICAL"]["top3_mean"],
                    "llm_calls": cost.get("calls"),
                    "llm_cost_usd": cost.get("cost_usd"),
                    "llm_latency_ms": cost.get("latency_ms"),
                    "llm_purposes": cost.get("purposes"),
                    "obligation_attribution": cost.get("obligation_attribution"),
                }
            )
            logger.info(
                "%s %s 기록 %s건 / 재실행 %d건 (%s) 벡터1위 %.4f 비용 $%.4f",
                row["case_id"],
                per_case[-1]["routing_group"],
                per_case[-1]["recorded_chunks"],
                per_case[-1]["rerun_chunks"],
                reproduction,
                per_case[-1]["vector_top1"] or float("nan"),
                per_case[-1]["llm_cost_usd"] or 0.0,
            )

    scored = [c for c in per_case if c["reproduction"] == "REPRODUCED"]
    light = [c for c in per_case if c["routing_group"] == "LIGHTWEIGHT"]

    empty_light = [c["case_id"] for c in light if c["recorded_chunks"] == 0]
    r1 = {
        "rule": "경량 후보 전건에서 검색 0건이어야 C 가 그대로 성립한다",
        "lightweight_cases": len(light),
        "empty_retrieval_cases": len(empty_light),
        "empty_case_ids": empty_light,
        "verdict": "PASS" if light and len(empty_light) == len(light) else "FAIL",
    }

    r2 = {
        metric: separability(scored, metric)
        for metric in ("hybrid_top1", "vector_top1", "lexical_top1")
    }

    full = [c for c in per_case if c["routing_group"] == "FULL"]
    total_cost = sum(c["llm_cost_usd"] or 0.0 for c in per_case)
    light_cost = sum(c["llm_cost_usd"] or 0.0 for c in light)
    light_mean = light_cost / len(light) if light else 0.0
    full_mean = (sum(c["llm_cost_usd"] or 0.0 for c in full) / len(full)) if full else 0.0
    projected_light = OPERATIONAL_LIGHTWEIGHT_SHARE * light_mean
    projected_full = (1.0 - OPERATIONAL_LIGHTWEIGHT_SHARE) * full_mean
    r3 = {
        "rule": "경량 경로가 LLM 0회일 때의 최대 절감",
        "attributed_total_usd": round(total_cost, 4),
        "result_file_total_usd": data["summary"].get("estimated_cost_usd"),
        "lightweight_usd": round(light_cost, 4),
        "lightweight_share": round(light_cost / total_cost, 4) if total_cost else None,
        "lightweight_calls": sum(c["llm_calls"] or 0 for c in light),
        "total_calls": sum(c["llm_calls"] or 0 for c in per_case),
        "lightweight_latency_ms": sum(c["llm_latency_ms"] or 0 for c in light),
        "total_latency_ms": sum(c["llm_latency_ms"] or 0 for c in per_case),
        "mean_cost_usd": {
            "LIGHTWEIGHT": round(light_mean, 4),
            "FULL": round(full_mean, 4),
        },
        "mean_calls": {
            "LIGHTWEIGHT": round(sum(c["llm_calls"] or 0 for c in light) / len(light), 2)
            if light
            else None,
            "FULL": round(sum(c["llm_calls"] or 0 for c in full) / len(full), 2) if full else None,
        },
        # 골든셋 비율(11/42)은 운영 비율이 아니다. 환산값에는 **케이스당 비용이 운영에서도
        # 같다**는 가정이 들어간다 — 골든 FULL 31건 중 20건이 IMPACT 이므로 그 평균은
        # 운영의 일부개정 평균보다 비쌀 수 있다. 그러면 이 환산은 절감을 과대평가한다.
        "projected_operational": {
            "lightweight_share_of_events": OPERATIONAL_LIGHTWEIGHT_SHARE,
            "share_of_cost": round(projected_light / (projected_light + projected_full), 4)
            if (projected_light + projected_full)
            else None,
            "assumption": "케이스당 비용이 운영에서도 이 42건과 같다",
        },
    }

    report = {
        "result_file": result_name,
        "model": model,
        "as_of": AS_OF.isoformat(),
        "cases": len(per_case),
        "reproduction": {
            v: sum(1 for c in per_case if c["reproduction"] == v)
            for v in ("REPRODUCED", "DRIFT", "NO_RECORD")
        },
        "classification": classification_crosstab(golden),
        "R1_empty_retrieval": r1,
        "R2_score_separability": r2,
        "R3_savings_ceiling": r3,
        "per_case": per_case,
    }
    logger.info("재현: %s", report["reproduction"])
    logger.info("R-1 검색 0건: %s", json.dumps(r1, ensure_ascii=False))
    for metric, verdict in r2.items():
        logger.info("R-2 %s: %s", metric, json.dumps(verdict, ensure_ascii=False))
    logger.info("R-3 절감 상한: %s", json.dumps(r3, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="라우팅 사전 확인 (LLM 미호출)")
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_dotenv()

    result_path = args.result or latest_result()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out or RESULTS_DIR / f"routing-precheck-{stamp}.json"
    data = json.loads(result_path.read_text(encoding="utf-8"))
    report = asyncio.run(run(result_path.name, data))
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("결과: %s", out_path)


if __name__ == "__main__":
    main()
