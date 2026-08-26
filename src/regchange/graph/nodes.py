"""그래프 노드 — 3단계·4단계의 단계 함수를 감싼다. 노드는 조립이지 로직이 아니다.

목적:
    `load_change → sanitize_input → retrieve_policy → extract_obligations → draft_impact
    → verify_citations → (재작성 | 적재) → human_review → (발송 대상 | 반려 기록)` 의
    각 지점을 함수로 정의한다.

구현 이유:
    **노드가 로직을 갖지 않는다.** 각 노드는 이미 그래프 없이 만들어 테스트한 함수를 부르고
    상태를 갱신할 뿐이다 (4단계 지시 §1 — "그래프 조립 전까지 LangGraph 없이 만들어라").
    이렇게 해야 결과가 이상할 때 **프레임워크 문제인지 로직 문제인지** 구별할 수 있다.

    **`sanitize_input` 이 R-23 을 해결하지 않는다.** 신뢰 등급을 태깅하되 스캔 범위를
    제한하지 않는다. 범위 제한은 `policy_document` 에 신뢰 등급이 내려온 뒤(5단계)이며,
    순서를 뒤집으면 감도를 두 번 맞추게 된다 (R-23 의 조치 순서). 지금 이 노드가 하는 일은
    **어느 조각이 외부에서 왔는지를 상태에 남기는 것**이고, 그것이 5단계의 입력이 된다.

    **검색이 추출보다 먼저다.** 4단계 지시의 노드 그림은 `extract_obligations →
    retrieve_policy` 순이지만, 실제 데이터 의존은 반대다 — 추출 프롬프트가 후보 문단을
    받아야 인용할 대상이 생기고, 후보 없이 추출하면 모델이 자기가 아는 조항을 지어낸다
    (3단계 `pipeline/obligations.py` 구현 이유). **그림이 아니라 의존을 따랐다.**
    ADR-013 의 ①②는 개념적 순서이고 이 파일은 실행 순서다.

    **승인 이후 노드만 쓰기 커넥션을 본다.** `enqueue_actions` 와 `record_rejection` 만
    `deps.review_conn`(`app_review`)을 쓰고, 나머지 노드는 `deps.graph_conn`(`app_graph`)만
    본다. `app_graph` 는 `review_decision` 과 `action_outbox` 에 INSERT 할 수 없으므로,
    **LLM 이 관여하는 노드가 승인이나 발송 대상을 만드는 경로는 권한 수준에서 없다**
    (원칙 5). 지금은 같은 프로세스의 다른 커넥션이며, 프로세스 분리는 5단계 발송 워커다.

    **기한을 우리가 정하지 않는다.** `due_at` 은 개정 조문의 시행일이며, 없으면 NULL 로
    둔다. "검토는 N일 안에" 같은 상수를 만들 근거가 이 저장소에 없다 (마이그레이션 011 §5).

트레이드오프:
    - 노드가 얇아 파일이 길어 보이지만 실제 로직은 전부 `pipeline/` 에 있다. 그 대신
      그래프를 빼도 파이프라인이 그대로 돌고, 파이프라인 테스트가 그래프 없이 돈다.
    - 커넥션을 `GraphDeps` 로 주입받는다. LangGraph 의 `config` 로 넘기는 방식이 더
      관용적이지만, `config` 는 직렬화 경로에 얹혀 있고 커넥션은 직렬화할 수 없다.
      **체크포인트에 들어가면 안 되는 것은 상태에 두지 않는다.**
    - 상태를 사전으로 주고받으므로 노드마다 복원 비용이 든다 (`graph/state.py` 참조).

엣지 케이스:
    - **검색 0건**: 노드는 실패하지 않는다. 후보 0건인 채로 추출과 초안이 돌고, gate 가
      전부 폐기해 `INSUFFICIENT_EVIDENCE` 가 된다. 0건과 "영향 없음"은 다른 값이다.
    - **초안 생성 실패**: `draft_failed=True` 로 상태에 남고 검증을 건너뛰어 적재로 간다.
      실패도 행으로 남아야 하기 때문이다.
    - **검증 호출 실패**: 그 평가를 이관하되 `verification_error` 를 상태에 남긴다.
      "근거가 없어서 이관"과 "검증하지 못해서 이관"은 조치가 다르다.
    - **재개 시 `human_review` 재실행**: `interrupt()` 가 resume 값을 돌려주므로 노드가
      다시 돌아도 사람에게 다시 묻지 않는다. **노드 앞에서 부수효과를 내지 않는 이유가
      이것이다** — 재실행되기 때문이다.
    - **이미 결정된 건에 다시 resume**: `record_decision` 이 `ReviewError` 를 던진다.
      두 번째 승인이 발송 대상을 하나 더 만들지 않는다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
from langgraph.graph import END
from langgraph.types import interrupt

from regchange.adapters.embedding import EmbeddingClient
from regchange.adapters.llm import LLMClient, LLMError
from regchange.adapters.storage import DocumentStore
from regchange.graph.state import (
    AssessmentState,
    dump_draft,
    dump_judgments,
    dump_retrieval,
    load_draft,
    load_judgments,
    load_retrieval,
)
from regchange.guards import injection, trust
from regchange.guards.killswitch import SwitchGate
from regchange.guards.trust import TrustLevel
from regchange.pipeline.impact import (
    MAX_REVISIONS,
    AssessmentStatus,
    DraftContext,
    GroundingMode,
    draft_once,
    finalize,
    verify_draft,
)
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval.models import SearchMode
from regchange.retrieval.promote import DEFAULT_TOP_N, promote_by_delegation
from regchange.retrieval.query import build_query
from regchange.retrieval.search import search
from regchange.review.models import DecisionRequest, ReviewDecisionKind
from regchange.review.queue import insert_assessment, record_decision

logger = logging.getLogger(__name__)

TRUST_UNTRUSTED = TrustLevel.UNTRUSTED.value
"""외부에서 온 텍스트 — 법제처 API 가 준 개정 조문. 우리가 쓴 것이 아니다."""

TRUST_INTERNAL = TrustLevel.TRUSTED.value
"""사내 문서 — 검색된 규정 문단. 우리 문서다.

**어휘를 스키마와 맞췄다 (2026-08-21).** 4단계에서는 `UNTRUSTED`/`INTERNAL` 이라는 이
모듈만의 문자열이었고, 그때는 이 태깅이 아무것도 가르지 않았으므로(R-23) 어휘가 갈려도
드러나지 않았다. 이제 등급은 `regulation_document.trust_level` / `policy_document.trust_level`
(마이그레이션 012)이 선언하며, 상태에 남는 값도 같은 값이어야 감사 질의가 둘을 잇는다."""


@dataclass(frozen=True, slots=True)
class GraphDeps:
    """노드가 쓰는 외부 자원. 상태가 아니라 주입이다.

    목적:
        커넥션·클라이언트처럼 직렬화할 수 없는 것을 노드에 전달한다.

    구현 이유:
        `graph_conn` 과 `review_conn` 을 나눠 받는다. 전자는 `app_graph`(읽기 + 기록 두
        테이블 + 초안 적재), 후자는 `app_review`(승인 레코드와 발송 대상)다. **승인 이후
        노드만 후자를 본다** — 경계가 코드의 조건문이 아니라 DB 권한에 있다 (원칙 5).

    트레이드오프:
        커넥션 두 개를 열어 둔다. 하나로 합치면 role 도 하나가 되고, 그러면 LLM 노드가
        승인 레코드를 쓸 수 있는 상태가 된다. 자원보다 경계가 비싸다.

    엣지 케이스:
        - `review_conn` 이 `app_graph` 로 열린 경우: 승인 노드에서 DB 가 거부한다.
          이 클래스가 검사하지 않는 이유는 **DB 가 단일 진실**이기 때문이다.
    """

    graph_conn: psycopg.AsyncConnection[Any]
    review_conn: psycopg.AsyncConnection[Any]
    switches: SwitchGate
    """킬 스위치 게이트 (5단계). 노드가 진입에서 이것을 본다.

    기본값을 두지 않는다. 기본값이 있으면 조립을 빠뜨린 실행이 **켜진 상태로** 돌고,
    그 사실은 스위치를 실제로 꺼 보기 전까지 드러나지 않는다."""
    llm: LLMClient
    embedding: EmbeddingClient
    store: DocumentStore
    as_of: dt.date
    top_k: int = 10
    mode: SearchMode = SearchMode.HYBRID
    promote: bool = True
    promote_top_n: int = DEFAULT_TOP_N
    grounding: GroundingMode = GroundingMode.ANCHORED
    """gate 3단을 어느 검증기로 돌 것인가.

    기본값이 `ANCHORED` 인 이유는 **측정이 기본값을 바꾸기 때문**이다. 대조 결과가
    나오기 전에 기본값을 옮기면 무엇이 달라졌는지의 기준선이 사라진다."""
    contexts: dict[str, DraftContext] = field(default_factory=dict)
    """평가 id → 컨텍스트. **체크포인트에 들어가지 않는 파생값**을 담는 캐시다.

    컨텍스트는 상태(검색 결과·의무 목록)에서 결정론적으로 다시 만들 수 있으므로 캐시가
    없어도 동작한다 — 프로세스가 재시작되면 다음 노드가 다시 만든다. 상태에 넣지 않는
    이유는 같은 사실이 두 곳에 있게 되기 때문이다."""


async def load_change(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """입력을 정규화하고 평가 id 와 스레드 id 를 확정한다.

    부수효과를 내지 않는다. 이 노드가 DB 를 건드리면 재개할 때마다 다시 건드린다.
    """
    assessment_id = state.get("assessment_id") or str(uuid4())
    thread_id = state.get("thread_id") or assessment_id
    logger.info(
        "load_change: %s %s (assessment=%s)",
        state.get("law_name"),
        state.get("article_path"),
        assessment_id,
    )
    return {
        "assessment_id": assessment_id,
        "thread_id": thread_id,
        "as_of": state.get("as_of") or deps.as_of.isoformat(),
        "revision": 0,
        "document_versions": state.get("document_versions") or {},
    }


async def sanitize_input(
    state: AssessmentState,
    deps: GraphDeps,  # noqa: ARG001 — 노드 서명을 통일한다. partial 이 키워드로 주입한다
) -> dict[str, Any]:
    """외부 텍스트에 신뢰 등급을 태깅하고 지시 유도 패턴을 스캔한다.

    개정 조문만 스캔한다 — 그것이 유일한 외부 텍스트이기 때문이다. 이 노드는 4단계부터
    이미 그렇게 동작했고, 사내 문단까지 훑던 `pipeline/` 두 곳이 R-23 이었다.
    **2026-08-21 그 두 곳을 고쳤고, 이제 스캔은 `wrap_external` 한 곳에서만 일어난다.**
    여기서 한 번 더 훑는 이유는 그래프 상태에 신호를 남겨 검토 화면과 감사 질의가
    프롬프트 조립을 기다리지 않고 볼 수 있게 하기 위해서다 — 같은 텍스트를 같은 패턴으로
    보므로 결과가 갈리지 않는다.
    """
    after_text = str(state.get("after_text") or "")
    signals = (
        sorted(injection.scan(trust.from_regulation(after_text, label="amended_article")))
        if after_text.strip()
        else []
    )
    if signals:
        logger.warning("개정 조문에서 지시 유도 패턴 신호: %s", ", ".join(signals))
    return {
        "injection_signals": signals,
        "trust_levels": {
            "amended_article": TRUST_UNTRUSTED,
            "policy_candidates": TRUST_INTERNAL,
        },
    }


async def retrieve_policy(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """사내 규정 후보를 검색하고 위임 승격을 적용한다 (R-22).

    승격을 검색 뒤에 별도로 적용하는 이유는 `retrieval/promote.py` 참조 — 1차 검색을
    고치지 않고 후보를 **추가**하는 조작이며, 그래서 기존 측정과 비교가 유지된다.
    """
    query = build_query(
        law_name=str(state["law_name"]),
        article_path=str(state.get("article_path") or ""),
        after_text=str(state["after_text"]),
    )
    as_of = dt.date.fromisoformat(str(state["as_of"]))
    result = await search(
        deps.graph_conn,
        switches=deps.switches,
        query=query,
        mode=deps.mode,
        limit=deps.top_k,
        as_of=as_of,
        client=deps.embedding,
    )
    if deps.promote:
        result = await promote_by_delegation(
            deps.graph_conn,
            switches=deps.switches,
            result=result,
            query=query,
            as_of=as_of,
            top_n=deps.promote_top_n,
            client=deps.embedding,
            mode=deps.mode,
        )
    logger.info(
        "retrieve_policy: 후보 %d건 (1차 %d + 승격 %d)",
        len(result.chunks),
        len(result.primary),
        len(result.promoted),
    )
    return {"retrieval": dump_retrieval(result)}


async def extract_obligations_node(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """개정 조문에서 의무사항을 뽑고 gate 2단을 적용한다 (3단계 재사용).

    검색 결과를 **주입한다.** 여기서 다시 검색하면 같은 실행 안에 후보 집합이 둘 생기고,
    인용 검증의 정답 집합이 어느 쪽인지 모호해진다 — 승격이 붙으면서 그 위험이 실재한다.
    """
    retrieval = load_retrieval(dict(state["retrieval"]))
    article = AmendedArticle(
        law_name=str(state["law_name"]),
        article_path=str(state.get("article_path") or ""),
        revision_kind=str(state.get("revision_kind") or ""),
        change_type=str(state.get("change_type") or ""),
        after_text=str(state["after_text"]),
        before_text=state.get("before_text"),
        document_versions=dict(state.get("document_versions") or {}),
    )
    outcome = await extract_obligations(
        deps.graph_conn,
        switches=deps.switches,
        article=article,
        llm=deps.llm,
        embedding=deps.embedding,
        store=deps.store,
        as_of=dt.date.fromisoformat(str(state["as_of"])),
        retrieval=retrieval,
    )
    gate = outcome.gate
    rows = [
        [o.obligation_type.value, o.summary, o.source_span]
        for o in (*gate.supported, *gate.unsupported)
    ]
    logger.info(
        "extract_obligations: %s (근거 있는 의무 %d / 없는 의무 %d / 인용 폐기 %d)",
        gate.status.value,
        len(gate.supported),
        len(gate.unsupported),
        len(gate.discarded),
    )
    return {
        "obligation_rows": rows,
        "obligation_status": gate.status.value,
        "obligation_discarded": [
            {
                "reason": d.reason.value,
                "paragraph_id": d.citation.paragraph_id,
                "claim": d.obligation_summary[:80],
            }
            for d in gate.discarded
        ],
    }


def _context(state: AssessmentState, deps: GraphDeps) -> DraftContext:
    """상태에서 평가 컨텍스트를 만든다. 캐시가 있으면 재사용하되 없어도 동작한다."""
    assessment_id = str(state["assessment_id"])
    cached = deps.contexts.get(assessment_id)
    if cached is not None:
        return cached
    ctx = DraftContext(
        assessment_id=UUID(assessment_id),
        law_name=str(state["law_name"]),
        article_path=str(state.get("article_path") or ""),
        revision_kind=str(state.get("revision_kind") or ""),
        change_type=str(state.get("change_type") or ""),
        after_text=str(state["after_text"]),
        obligation_rows=tuple(
            (str(row[0]), str(row[1]), str(row[2])) for row in state.get("obligation_rows") or []
        ),
        retrieval=load_retrieval(dict(state["retrieval"])),
        document_versions=dict(state.get("document_versions") or {}),
    )
    deps.contexts[assessment_id] = ctx
    return ctx


async def draft_impact(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """영향평가 초안을 만들고 코드 gate 둘(정합성·인용 실재)을 적용한다.

    재작성 회차(`revision`)일 때 이전 초안과 **검증기의 판정 문장만** 넘긴다.
    검증기의 프롬프트나 추론은 넘기지 않는다 (원칙 3).
    """
    ctx = _context(state, deps)
    revision = int(state.get("revision") or 0)
    grounding = load_judgments(state.get("judgments"))
    step = await draft_once(
        deps.graph_conn,
        switches=deps.switches,
        ctx=ctx,
        llm=deps.llm,
        store=deps.store,
        revision=revision,
        previous_raw=state.get("previous_raw"),
        unsupported_notes=grounding.unsupported_notes if revision else (),
    )
    return {
        "draft": dump_draft(step.draft),
        "previous_raw": step.raw_output,
        "draft_failed": step.failed,
        "consistency_violations": [
            {"rule": v.rule.value, "claim": v.claim[:80], "detail": v.detail}
            for v in step.consistency.violations
        ],
        "gate_discarded": [
            {
                "kind": d.kind.value,
                "reason": d.reason.value,
                "label": d.label,
                "paragraph_id": d.paragraph_id,
            }
            for d in step.gate.discarded
        ],
        "injection_signals": sorted(
            {*(state.get("injection_signals") or []), *step.injection_signals}
        ),
    }


async def verify_citations(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """Gate 3단 — 주장마다 독립 컨텍스트로 뒷받침 여부를 판정한다.

    초안 생성이 실패했으면 판정할 것이 없다. 그 경우 빈 판정을 남기고 넘어간다 —
    실패를 검증 실패로 위장하지 않는다.
    """
    if state.get("draft_failed"):
        return {"judgments": [], "verification_error": None}

    draft = load_draft(state.get("draft"))
    if draft is None:
        return {"judgments": [], "verification_error": None}

    ctx = _context(state, deps)
    try:
        run = await verify_draft(
            deps.graph_conn,
            switches=deps.switches,
            ctx=ctx,
            draft=draft,
            llm=deps.llm,
            store=deps.store,
            revision=int(state.get("revision") or 0),
            mode=deps.grounding,
        )
    except LLMError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.exception("gate 3단 검증이 실패했다. 이 평가는 이관한다: %s", detail)
        return {"judgments": [], "verification_error": detail}

    logger.info(
        "verify_citations: %s",
        {level.value: count for level, count in run.grounding.counts.items()},
    )
    return {"judgments": dump_judgments(run.grounding), "verification_error": None}


def grounding_gate(state: AssessmentState) -> str:
    """근거가 충분한가 — 재작성할 것인가, 적재로 갈 것인가 (조건 분기).

    목적:
        evaluator-optimizer 의 루프를 그래프 간선으로 표현한다.

    구현 이유:
        **순수 함수다.** 상태만 보고 다음 노드 이름을 돌려준다. 분기 안에서 부수효과를
        내면 재개 시 그 효과가 다시 일어난다.

        재작성 상한은 `MAX_REVISIONS`(ADR-013 이 정한 1)이다. 상한에 닿으면 재작성하지
        않고 적재로 간다 — 남은 주장은 `finalize` 가 제거하며, 근거가 하나도 없으면
        `INSUFFICIENT_EVIDENCE` 로 이관된다.

    트레이드오프:
        검증 실패(`verification_error`)와 근거 부족을 같은 방향(적재)으로 보낸다. 둘을
        다른 노드로 보내면 그래프가 커지는데, 실제 조치의 차이는 **기록된 사유**에 있지
        경로에 있지 않다.

    엣지 케이스:
        - 초안 생성 실패: 재작성하지 않는다. 스키마를 못 지키는 상태에서 다시 쓰는 것은
          우연에 기대는 일이다.
        - 검증 실패: 재작성하지 않는다. 판정을 받지 못했으므로 무엇을 고칠지 모른다.
    """
    if state.get("draft_failed") or state.get("verification_error"):
        return "persist_assessment"
    grounding = load_judgments(state.get("judgments"))
    if grounding.needs_rewrite and int(state.get("revision") or 0) < MAX_REVISIONS:
        return "rewrite"
    return "persist_assessment"


async def bump_revision(
    state: AssessmentState,
    deps: GraphDeps,  # noqa: ARG001 — 노드 서명을 통일한다
) -> dict[str, Any]:
    """재작성 회차를 올린다. 이 노드가 있는 이유는 회차 증가가 **상태 변경**이기 때문이다.

    분기 함수 안에서 올리면 순수하지 않게 되고, 초안 노드 안에서 올리면 첫 초안과 재작성이
    같은 회차로 기록된다 — 그러면 `llm_invocation.revision` 이 신호가 되지 못한다.
    """
    revision = int(state.get("revision") or 0) + 1
    grounding = load_judgments(state.get("judgments"))
    logger.info(
        "뒷받침되지 않은 주장 %d건 — 재작성 %d회차 (상한 %d)",
        len(grounding.unsupported_keys),
        revision,
        MAX_REVISIONS,
    )
    return {"revision": revision}


async def persist_assessment(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """최종 판정을 코드가 정하고 평가를 적재한다. 큐에 넣을지도 여기서 정해진다.

    `INSUFFICIENT_EVIDENCE` 는 큐에 넣지 않되 **행은 남긴다.** 이관 비율(ADR-013 신호 4번)을
    세려면 그 행이 있어야 한다.
    """
    draft = load_draft(state.get("draft"))
    grounding = load_judgments(state.get("judgments"))
    if draft is None:
        msg = "적재할 초안이 없다 — draft_impact 가 상태를 남기지 않았다"
        raise RuntimeError(msg)

    final_draft, status = finalize(draft, grounding, draft_status=draft.status)
    queued = status is not AssessmentStatus.INSUFFICIENT_EVIDENCE
    due_at = _due_at(state)

    reason = final_draft.reason
    if state.get("verification_error"):
        reason = f"{reason}\n[검증 실패] {state['verification_error']}".strip()

    async with deps.graph_conn.transaction():
        await insert_assessment(
            deps.graph_conn,
            assessment_id=UUID(str(state["assessment_id"])),
            thread_id=str(state["thread_id"]),
            law_name=str(state["law_name"]),
            article_path=str(state.get("article_path") or ""),
            revision_kind=str(state.get("revision_kind") or ""),
            change_type=str(state.get("change_type") or ""),
            as_of=dt.date.fromisoformat(str(state["as_of"])),
            status=status.value,
            obligation_type=final_draft.obligation_type.value,
            risk_level=final_draft.risk_level.value,
            confidence=final_draft.confidence.value,
            summary=final_draft.summary,
            reason=reason,
            revisions=int(state.get("revision") or 0),
            draft=dump_draft(final_draft),
            grounding={
                "counts": {level.value: count for level, count in grounding.counts.items()},
                "unsupported": list(grounding.unsupported_keys),
                "notes": list(grounding.unsupported_notes),
                "unsupported_ratio": grounding.unsupported_ratio,
                "judgments": dump_judgments(grounding),
            },
            discarded=[
                *(state.get("gate_discarded") or []),
                *(state.get("consistency_violations") or []),
            ],
            queued=queued,
            due_at=due_at,
        )
    # **노드가 트랜잭션 경계다.** 노드가 끝나면 그 노드가 만든 사실이 커밋되어 있어야
    # 한다 — 체크포인트는 저장됐는데 DB 에 행이 없으면, 재개한 그래프가 존재하지 않는
    # 평가를 승인하려 든다. psycopg 는 이미 트랜잭션이 열려 있으면 `transaction()` 을
    # 세이브포인트로 만들므로 여기서 명시적으로 커밋한다.
    await deps.graph_conn.commit()

    return {
        "draft": dump_draft(final_draft),
        "status": status.value,
        "persisted": True,
        "queued": queued,
    }


def route_after_persist(state: AssessmentState) -> str:
    """큐에 넣은 건만 사람에게 간다. 이관된 건은 여기서 끝난다."""
    return "human_review" if state.get("queued") else END


def human_review(state: AssessmentState) -> dict[str, Any]:
    """승인 게이트 — 그래프가 여기서 멈춘다 (원칙 4).

    목적:
        사람의 판단 없이는 다음 노드가 존재하지 않는 상태를 만든다.

    구현 이유:
        UI 레이어의 검사가 아니라 `interrupt` 다. UI 검사는 API 를 직접 호출하면 우회되지만,
        그래프가 중단되어 있으면 **우회 경로 자체가 없다.** 재개는 `Command(resume=...)`
        로만 가능하고, 재개는 체크포인트가 있어야 성립한다.

    트레이드오프:
        이 노드는 재개 시 **다시 실행된다.** 그래서 앞에서 부수효과를 내면 안 되고,
        `interrupt()` 호출 이전에 어떤 쓰기도 두지 않았다.

    엣지 케이스:
        - 체크포인터가 없으면 `interrupt()` 가 실패한다. 그것이 맞다 — 상태를 저장하지
          못하는 그래프에서 승인 대기는 성립하지 않는다.
        - 재개 값이 판단 형식이 아님: 다음 노드에서 검증이 실패하고 예외가 오른다.
          조용히 승인으로 해석하지 않는다.
    """
    decision = interrupt(
        {
            "assessment_id": state.get("assessment_id"),
            "law_name": state.get("law_name"),
            "article_path": state.get("article_path"),
            "status": state.get("status"),
            "summary": (state.get("draft") or {}).get("summary"),
            "risk_level": (state.get("draft") or {}).get("risk_level"),
            "awaiting": "ACCEPT | EDIT | REJECT",
        }
    )
    return {"decision": dict(decision) if decision else None}


def route_after_review(state: AssessmentState) -> str:
    """수락·수정은 발송 대상을 만들고, 반려는 기록만 남긴다."""
    decision = state.get("decision") or {}
    kind = str(decision.get("decision", "")).upper()
    return "record_rejection" if kind == ReviewDecisionKind.REJECT.value else "enqueue_actions"


async def enqueue_actions(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """승인을 기록하고 발송 대상을 만든다. 여기만 쓰기 커넥션을 본다.

    `app_review` 로 붙은 커넥션을 쓴다. `app_graph` 로 이 노드가 돌면 DB 가 거부한다 —
    경계가 이 함수의 조건문이 아니라 권한에 있다 (원칙 5).
    """
    return await _record(state, deps)


async def record_rejection(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """반려를 기록한다. 발송 대상은 만들어지지 않는다.

    반려도 결정이며 이력이다. 기록하지 않으면 "반려된 건"과 "처리되지 않은 건"이 같은
    부재가 되고, 반려율(F-7 감시 지표)을 셀 수 없다.
    """
    return await _record(state, deps)


async def _record(state: AssessmentState, deps: GraphDeps) -> dict[str, Any]:
    """승인·반려를 한 트랜잭션으로 남긴다. 발송 대상 생성은 `review/queue.py` 가 정한다."""
    payload = state.get("decision") or {}
    request = DecisionRequest.model_validate(payload)
    decision_id, outbox_ids = await record_decision(
        deps.review_conn,
        assessment_id=UUID(str(state["assessment_id"])),
        request=request,
    )
    # 승인은 그래프가 끝나기 전에 확정되어야 한다 (위 `persist_assessment` 와 같은 규칙).
    await deps.review_conn.commit()
    return {
        "decision_id": str(decision_id),
        "outbox_ids": [str(i) for i in outbox_ids],
    }


def _due_at(state: AssessmentState) -> dt.datetime | None:
    """기한 — **개정 조문의 시행일**. 확보하지 못했으면 None 이며 그 사실이 따로 세어진다.

    임의의 기본 기한으로 채우지 않는다. 근거 없는 숫자가 운영 지표가 되면, 그 지표를 보고
    한 판단도 근거가 없다 (마이그레이션 011 §5).
    """
    raw = state.get("document_versions", {}).get("effective_date")
    if not raw:
        return None
    try:
        return dt.datetime.combine(dt.date.fromisoformat(str(raw)), dt.time.min, tzinfo=dt.UTC)
    except ValueError:
        logger.warning("시행일을 읽을 수 없다: %r — 기한을 미상으로 둔다", raw)
        return None
