"""그래프를 끝까지 돌린다 — 승인 대기에서 멈추고, **프로세스를 넘어 살아남고**, 재개된다.

이 테스트가 존재하는 이유: 원칙 4 는 프레임워크 동작에 의존한다. `interrupt` 가 실제로
멈추는지, 체크포인트가 프로세스 재시작을 넘어 남는지, 재개가 승인 레코드와 발송 대상을
만드는지를 고정하지 않으면, 프레임워크 업그레이드가 승인 게이트를 조용히 없앨 수 있다
(ADR-013 엣지 케이스 — "프레임워크 동작에 대한 잘못된 가정").

실제 모델을 부르지 않는다 (CLAUDE.md §6).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.llm.base import JsonSchema, LLMResult
from regchange.adapters.storage.local import LocalDocumentStore
from regchange.graph.build import CHECKPOINT_SCHEMA, build_graph
from regchange.graph.nodes import GraphDeps
from regchange.graph.runner import GraphRunner
from regchange.guards.killswitch import Switch, SwitchGate, static_gate
from regchange.retrieval.index import embed_corpus
from regchange.store.dsn import DbRole, assert_disposable_database, role_dsn

pytestmark = pytest.mark.requires_db

AS_OF = dt.date(2026, 2, 1)
NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)

PARAGRAPH_TEXT = (
    "① 정보보호부장은 침해사고를 인지한 즉시 과학기술정보통신부장관 또는 "
    "한국인터넷진흥원에 신고한다."
)
QUOTE = "정보보호부장은 침해사고를 인지한 즉시"

TEST_DATABASE = "regchange_test"
"""`tests/conftest.py` 와 같은 값. tests 는 패키지가 아니라 import 할 수 없다."""


class StubEmbedding:
    """결정론적 벡터. 순위 자체는 이 테스트의 관심사가 아니다."""

    model_id = "stub:embedding"
    dimensions = 4

    def embed_documents(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(t) for t in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        return (1.0, (sum(ord(c) for c in text) % 97) / 100.0, 0.0, 0.0)


class StubLLM:
    """프롬프트 id 로 응답을 고르는 스텁. 호출 순서를 테스트가 몰라도 된다."""

    model_id = "stub:model"

    def __init__(self, by_prompt: dict[str, list[dict[str, Any]]]) -> None:
        self._by_prompt = {k: list(v) for k, v in by_prompt.items()}
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        prompt_id: str,
        prompt_version: str,  # noqa: ARG002 — Protocol 계약
        system: str,  # noqa: ARG002
        user_content: str,
        response_schema: JsonSchema,  # noqa: ARG002
    ) -> LLMResult:
        self.calls.append(prompt_id)
        self.last_user_content = user_content
        queue = self._by_prompt[prompt_id]
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return LLMResult(
            output=payload,
            raw_text=json.dumps(payload, ensure_ascii=False),
            model_id=self.model_id,
            api_version="test-version",
            request_params={"model": self.model_id},
            stop_reason="end_turn",
            latency_ms=5,
            input_tokens=10,
            output_tokens=5,
        )


def _obligation(paragraph_id: str) -> dict[str, Any]:
    return {
        "status": "OK",
        "reason": "",
        "suggested_action": "NONE",
        "obligations": [
            {
                "obligation_type": "STRENGTHENED",
                "summary": "신고 기한이 24시간 이내로 명문화됐다",
                "source_span": "제48조의3제1항",
                "citations": [
                    {"paragraph_id": paragraph_id, "quote": QUOTE, "start": 0, "end": len(QUOTE)}
                ],
            }
        ],
    }


def _draft(paragraph_id: str) -> dict[str, Any]:
    return {
        "status": "DRAFT",
        "obligation_type": "STRENGTHENED",
        "risk_level": "HIGH",
        "risk_reason": "대외 신고 기한이 명문화됐다",
        "confidence": "MEDIUM",
        "summary": "신고 기한 조항을 24시간 기준으로 정비해야 한다",
        "reason": "",
        "impacts": [
            {
                "paragraph_id": paragraph_id,
                "quote": QUOTE,
                "claim": "이 조항의 신고 시점을 24시간 기준으로 고쳐야 한다",
                "obligation_index": 0,
                "control_items": ["신고 시점 문구 정비"],
            }
        ],
        "departments": [
            {
                "department": "정보보호부",
                "basis_paragraph_id": paragraph_id,
                "basis_quote": QUOTE,
                "derivation": "SUBJECT_IN_TEXT",
                "rationale": "조항이 정보보호부장을 주체로 명시한다",
            }
        ],
        "required_evidence": ["신고 이력"],
    }


SUPPORTED = {"level": "SUPPORTED", "reason": "문단이 주장의 핵심을 담고 있다"}


async def seed_corpus(conn: psycopg.AsyncConnection[Any]) -> UUID:
    """문서 하나와 문단 하나를 넣고 문단 id 를 돌려준다."""
    document_id, paragraph_id = uuid4(), uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO policy_document (
                id, doc_id, version, title, owner_dept, classification,
                effective_date, source_path, source_sha256, known_from
            ) VALUES (%s, 'ISP-PROC-002', '2.4', '침해사고 대응절차', '정보보호부',
                      'INTERNAL', DATE '2025-06-01', 'p.md', repeat('a', 64), %s)
            """,
            (document_id, NOW),
        )
        await cur.execute(
            """
            INSERT INTO policy_paragraph (
                id, document_id, article_no, article_title, seq_in_doc,
                text_raw, text_norm, text_norm_sha256, norm_rule_version, known_from
            ) VALUES (%s, %s, 7, '침해사고 신고', 7, %s, %s, repeat('b', 64), 'norm-v2', %s)
            """,
            (paragraph_id, document_id, PARAGRAPH_TEXT, PARAGRAPH_TEXT, NOW),
        )
    await conn.commit()
    return paragraph_id


def payload(assessment_id: str) -> dict[str, Any]:
    """그래프 입력. 시행일을 넣어 기한이 실제로 채워지는지도 함께 본다."""
    return {
        "assessment_id": assessment_id,
        "thread_id": assessment_id,
        "law_name": "정보통신망 이용촉진 및 정보보호 등에 관한 법률",
        "article_path": "제48조의3",
        "revision_kind": "일부개정",
        "change_type": "MODIFIED",
        "after_text": "침해사고 발생 사실을 알게 된 때부터 24시간 이내에 신고하여야 한다.",
        "before_text": "침해사고가 발생하면 즉시 신고하여야 한다.",
        "as_of": AS_OF.isoformat(),
        "document_versions": {"effective_date": "2026-09-11"},
    }


def checkpoint_test_dsn() -> str:
    """테스트 DB 의 체크포인트 스키마로 붙는 DSN.

    운영 헬퍼(`graph.build.checkpoint_dsn`)는 운영 DB 를 본다. 테스트가 그것을 쓰면
    **테스트가 운영 DB 에 체크포인트를 남긴다** — 사건 하나가 이미 그렇게 일어났다
    (`docs/incidents/test-truncated-operations-history.md`).
    """
    parsed = urlsplit(role_dsn(DbRole.GRAPH))
    dsn = urlunsplit(parsed._replace(path=f"/{assert_disposable_database(TEST_DATABASE)}"))
    return f"{dsn}?options=-c%20search_path%3D{CHECKPOINT_SCHEMA}"


async def make_runner(
    *,
    graph_conn: psycopg.AsyncConnection[Any],
    review_conn: psycopg.AsyncConnection[Any],
    llm: StubLLM,
    saver: Any,
    tmp_path: Path,
    switches: SwitchGate | None = None,
) -> GraphRunner:
    """의존성을 조립한 러너 하나. 재시작을 흉내 낼 때 이 함수를 다시 부른다."""
    deps = GraphDeps(
        graph_conn=graph_conn,
        review_conn=review_conn,
        switches=switches or static_gate({Switch.RETRIEVAL: True, Switch.LLM: True}),
        llm=llm,
        embedding=StubEmbedding(),  # type: ignore[arg-type]
        store=LocalDocumentStore(tmp_path),
        as_of=AS_OF,
        promote=False,
    )
    return GraphRunner(graph=build_graph(deps, checkpointer=saver), deps=deps)


async def _row(conn: psycopg.AsyncConnection[Any], sql: str, *args: Any) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
        return await cur.fetchone()


async def test_graph_stops_at_approval_and_resumes_after_restart(
    owner_conn: psycopg.AsyncConnection[Any],
    role_connect: Any,
    tmp_path: Path,
) -> None:
    """승인 대기에서 멈추고, 체크포인터를 새로 열어(재시작) 재개하면 발송 대상이 생긴다."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    paragraph_id = await seed_corpus(owner_conn)
    embedding = StubEmbedding()
    await embed_corpus(owner_conn, embedding)  # type: ignore[arg-type]
    await owner_conn.commit()

    graph_conn = await role_connect(DbRole.GRAPH)
    review_conn = await role_connect(DbRole.REVIEW)
    llm = StubLLM(
        {
            "obligation-extraction": [_obligation(str(paragraph_id))],
            "impact-assessment": [_draft(str(paragraph_id))],
            "citation-grounding": [SUPPORTED],
        }
    )
    assessment_id = str(uuid4())

    # ── 1차 실행: 승인 대기에서 멈춘다 ────────────────────────────────
    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        runner = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
        )
        result = await runner.start(payload(assessment_id))

    assert "__interrupt__" in result, "승인 게이트가 그래프를 멈추지 않았다"
    assert result["status"] == "OK"

    row = await _row(owner_conn, "SELECT * FROM impact_assessment WHERE id = %s", assessment_id)
    assert row is not None
    assert row["review_state"] == "PENDING"
    assert row["queued_at"] is not None
    assert row["due_at"].date() == dt.date(2026, 9, 11), "기한은 개정 조문의 시행일이다"

    outbox = await _row(owner_conn, "SELECT count(*) AS n FROM action_outbox")
    assert outbox is not None and outbox["n"] == 0, "승인 전에 발송 대상이 만들어졌다"

    # ── 재시작 흉내: 체크포인터도 그래프도 새로 만든다 ────────────────
    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        resumed_runner = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
        )
        final = await resumed_runner.resume(
            assessment_id,
            {"decision": "ACCEPT", "decided_by": "reviewer", "reviewed_ms": 4200},
        )

    assert final["decision_id"], "승인 레코드가 만들어지지 않았다"
    assert len(final["outbox_ids"]) == 1

    decision = await _row(
        owner_conn, "SELECT * FROM review_decision WHERE impact_assessment_id = %s", assessment_id
    )
    assert decision is not None
    assert decision["decision"] == "ACCEPT"
    assert decision["reviewed_ms"] == 4200, "검토 소요가 기록되지 않으면 F-7 을 감시할 수 없다"

    after = await _row(owner_conn, "SELECT * FROM impact_assessment WHERE id = %s", assessment_id)
    assert after is not None and after["review_state"] == "ACCEPTED"

    sent = await _row(
        owner_conn,
        "SELECT payload FROM action_outbox WHERE review_decision_id = %s",
        decision["id"],
    )
    assert sent is not None
    assert "prompt" not in json.dumps(sent["payload"]), "발송 대상에 프롬프트가 담겼다 (원칙 5)"


async def test_rejection_records_reason_and_makes_no_outbox(
    owner_conn: psycopg.AsyncConnection[Any],
    role_connect: Any,
    tmp_path: Path,
) -> None:
    """반려도 결정이다 — 승인 레코드는 남고 발송 대상은 만들어지지 않는다."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    paragraph_id = await seed_corpus(owner_conn)
    await embed_corpus(owner_conn, StubEmbedding())  # type: ignore[arg-type]
    await owner_conn.commit()

    graph_conn = await role_connect(DbRole.GRAPH)
    review_conn = await role_connect(DbRole.REVIEW)
    llm = StubLLM(
        {
            "obligation-extraction": [_obligation(str(paragraph_id))],
            "impact-assessment": [_draft(str(paragraph_id))],
            "citation-grounding": [SUPPORTED],
        }
    )
    assessment_id = str(uuid4())

    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        runner = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
        )
        await runner.start(payload(assessment_id))
        await runner.resume(
            assessment_id,
            {
                "decision": "REJECT",
                "decided_by": "reviewer",
                "reason_code": "WRONG_PARAGRAPH",
                "reason_note": "이 조항은 대외 신고가 아니라 사내 보고를 다룬다",
                "reviewed_ms": 90_000,
            },
        )

    decision = await _row(
        owner_conn, "SELECT * FROM review_decision WHERE impact_assessment_id = %s", assessment_id
    )
    assert decision is not None
    assert decision["decision"] == "REJECT"
    assert decision["reason_code"] == "WRONG_PARAGRAPH", "반려 사유가 구조화되지 않았다"

    outbox = await _row(owner_conn, "SELECT count(*) AS n FROM action_outbox")
    assert outbox is not None and outbox["n"] == 0

    after = await _row(owner_conn, "SELECT * FROM impact_assessment WHERE id = %s", assessment_id)
    assert after is not None and after["review_state"] == "REJECTED"


async def test_second_decision_creates_no_duplicate_dispatch(
    owner_conn: psycopg.AsyncConnection[Any],
    role_connect: Any,
    tmp_path: Path,
) -> None:
    """두 번째 결정이 발송 대상을 하나 더 만들지 않는다 — **두 겹으로 막힌다.**

    1. 그래프: 이미 끝난 스레드는 재개해도 승인 노드를 다시 실행하지 않는다.
    2. 승인 경로: `record_decision` 을 직접 불러도 `PENDING` 이 아니면 거부한다.

    두 번째가 중요하다. 첫 번째는 프레임워크 동작이고 두 번째는 우리 코드다 — 프레임워크
    동작이 바뀌어도 발송이 두 번 일어나지 않아야 한다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from regchange.review.models import DecisionRequest, ReviewDecisionKind
    from regchange.review.queue import ReviewError, record_decision

    paragraph_id = await seed_corpus(owner_conn)
    await embed_corpus(owner_conn, StubEmbedding())  # type: ignore[arg-type]
    await owner_conn.commit()

    graph_conn = await role_connect(DbRole.GRAPH)
    review_conn = await role_connect(DbRole.REVIEW)
    llm = StubLLM(
        {
            "obligation-extraction": [_obligation(str(paragraph_id))],
            "impact-assessment": [_draft(str(paragraph_id))],
            "citation-grounding": [SUPPORTED],
        }
    )
    assessment_id = str(uuid4())

    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        runner = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
        )
        await runner.start(payload(assessment_id))
        accept = {"decision": "ACCEPT", "decided_by": "reviewer", "reviewed_ms": 1000}
        await runner.resume(assessment_id, accept)
        # 1. 끝난 스레드를 다시 재개해도 승인 노드가 돌지 않는다.
        await runner.resume(assessment_id, accept)

    # 2. 승인 경로를 직접 불러도 거부된다.
    with pytest.raises(ReviewError, match="이미"):
        await record_decision(
            review_conn,
            assessment_id=UUID(assessment_id),
            request=DecisionRequest(
                decision=ReviewDecisionKind.ACCEPT, decided_by="reviewer", reviewed_ms=1
            ),
        )

    outbox = await _row(owner_conn, "SELECT count(*) AS n FROM action_outbox")
    assert outbox is not None and outbox["n"] == 1, "재승인이 발송 대상을 하나 더 만들었다"
    decisions = await _row(owner_conn, "SELECT count(*) AS n FROM review_decision")
    assert decisions is not None and decisions["n"] == 1


async def test_llm_switch_off_still_lets_a_pending_review_be_decided(
    owner_conn: psycopg.AsyncConnection[Any],
    role_connect: Any,
    tmp_path: Path,
) -> None:
    """`LLM_ENABLED` 를 꺼도 **승인 대기 중이던 건은 재개된다** (ADR-019 엣지 케이스).

    스위치는 모델을 멈추는 장치이지 **사람의 판단을 멈추는 장치가 아니다.** 승인 이후
    노드는 모델을 부르지 않으므로, 이미 사람 앞에 놓인 건은 계속 결정될 수 있어야 한다.
    이 성질은 코드를 읽으면 그럴듯하지만, 읽어서 그럴듯한 것과 실제로 도는 것은 다르다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    paragraph_id = await seed_corpus(owner_conn)
    embedding = StubEmbedding()
    await embed_corpus(owner_conn, embedding)  # type: ignore[arg-type]
    await owner_conn.commit()

    graph_conn = await role_connect(DbRole.GRAPH)
    review_conn = await role_connect(DbRole.REVIEW)
    llm = StubLLM(
        {
            "obligation-extraction": [_obligation(str(paragraph_id))],
            "impact-assessment": [_draft(str(paragraph_id))],
            "citation-grounding": [SUPPORTED],
        }
    )
    assessment_id = str(uuid4())

    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        runner = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
        )
        result = await runner.start(payload(assessment_id))
    assert "__interrupt__" in result

    # ── 여기서 스위치를 전부 끈다. 그래도 사람의 결정은 기록돼야 한다 ──
    async with AsyncPostgresSaver.from_conn_string(checkpoint_test_dsn()) as saver:
        await saver.setup()
        resumed = await make_runner(
            graph_conn=graph_conn,
            review_conn=review_conn,
            llm=llm,
            saver=saver,
            tmp_path=tmp_path,
            switches=static_gate({Switch.RETRIEVAL: False, Switch.LLM: False}),
        )
        final = await resumed.resume(
            assessment_id,
            {"decision": "ACCEPT", "decided_by": "reviewer", "reviewed_ms": 3100},
        )

    assert final["decision_id"], "스위치가 꺼졌다고 승인 기록까지 막혔다"
    assert len(final["outbox_ids"]) == 1, (
        "발송 **대상**은 만들어진다. 실제 발송을 막는 것은 DISPATCH_ENABLED 다"
    )

    after = await _row(owner_conn, "SELECT * FROM impact_assessment WHERE id = %s", assessment_id)
    assert after is not None and after["review_state"] == "ACCEPTED"
