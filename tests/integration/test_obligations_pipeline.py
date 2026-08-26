"""검색 → 추출 → gate → 기록 경로를 스텁 LLM 으로 끝까지 돌린다.

이 테스트가 존재하는 이유: 세 가지가 **첫 호출부터** 성립하는지를 고정한다.

1. **`llm_invocation` 이 첫 호출부터 남는다.** ADR-013 이 LangGraph 를 쓰는 조건으로 이
   기록을 걸었다. 나중에 붙이면 초기 호출들의 이력이 없고 그 조건이 거짓이 된다.
2. **gate 2단이 코드로 판정한다.** 지어낸 인용이 실제로 폐기되고, 폐기된 뒤 남은 근거가
   0건이면 `INSUFFICIENT_EVIDENCE` 가 된다 — 모델이 뭐라고 말하든.
3. **실패한 호출도 행으로 남는다.** 스키마 위반 재시도가 두 행이 되어야 재작성 비율을
   셀 수 있다 (ADR-013 신호 2).

실제 모델을 부르지 않는다 (CLAUDE.md §6 — LLM 호출은 단위/통합 테스트에서 스텁).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.llm.base import JsonSchema, LLMResult, SchemaViolationError
from regchange.adapters.storage.local import LocalDocumentStore
from regchange.guards.citations import DiscardReason, GateStatus
from regchange.guards.killswitch import SwitchGate
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval.index import embed_corpus
from regchange.retrieval.models import SearchMode

pytestmark = pytest.mark.requires_db

AS_OF = dt.date(2026, 2, 1)
NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)

PARAGRAPH_TEXT = (
    "① 정보보호부장은 침해사고를 인지한 즉시 과학기술정보통신부장관 또는 "
    "한국인터넷진흥원에 신고한다."
)


class StubEmbedding:
    """결정론적 벡터를 돌려주는 임베딩 스텁. 실제 모델을 부르지 않는다."""

    model_id = "stub:embedding"
    dimensions = 4

    def embed_documents(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        # 내용에 따라 조금씩 다른 벡터를 준다. 순위 자체는 이 테스트의 관심사가 아니다.
        seed = sum(ord(ch) for ch in text) % 97
        return (1.0, seed / 100.0, 0.0, 0.0)


class StubLLM:
    """미리 정한 응답을 순서대로 돌려주는 LLM 스텁."""

    model_id = "stub:model"

    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete(
        self,
        *,
        prompt_id: str,  # noqa: ARG002 — Protocol 계약을 지키기 위한 인자
        prompt_version: str,  # noqa: ARG002
        system: str,
        user_content: str,
        response_schema: JsonSchema,  # noqa: ARG002
    ) -> LLMResult:
        self.calls += 1
        self.last_user_content = user_content
        self.last_system = system
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LLMResult(
            output=nxt,
            raw_text=json.dumps(nxt, ensure_ascii=False),
            model_id=self.model_id,
            api_version="test-version",
            request_params={"model": self.model_id, "sampling": "unsupported_by_model"},
            stop_reason="end_turn",
            latency_ms=12,
            input_tokens=100,
            output_tokens=20,
        )


def _payload(paragraph_id: str | None, quote: str, *, status: str = "OK") -> dict[str, Any]:
    citations = (
        []
        if paragraph_id is None
        else [{"paragraph_id": paragraph_id, "quote": quote, "start": 0, "end": len(quote)}]
    )
    return {
        "status": status,
        "reason": "" if status == "OK" else "대응하는 사내 조항이 없다",
        "suggested_action": "NONE" if status == "OK" else "NEW_PROVISION_REVIEW",
        "obligations": [
            {
                "obligation_type": "STRENGTHENED",
                "summary": "신고 기한이 24시간 이내로 명문화됐다",
                "source_span": "제48조의3제1항",
                "citations": citations,
            }
        ],
    }


async def _seed_corpus(conn: psycopg.AsyncConnection[Any]) -> UUID:
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


ARTICLE = AmendedArticle(
    law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    article_path="제48조의3",
    revision_kind="일부개정",
    change_type="MODIFIED",
    after_text="침해사고 발생 사실을 알게 된 때부터 24시간 이내에 신고하여야 한다.",
    before_text="침해사고가 발생하면 즉시 신고하여야 한다.",
    document_versions={"ISP-PROC-002": "2.4"},
)


async def _run(
    conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
    llm: StubLLM,
    switches_on: SwitchGate,
) -> Any:
    embedding = StubEmbedding()
    await embed_corpus(conn, embedding)  # type: ignore[arg-type]
    await conn.commit()
    return await extract_obligations(
        conn,
        switches=switches_on,
        article=ARTICLE,
        llm=llm,
        embedding=embedding,  # type: ignore[arg-type]
        store=LocalDocumentStore(tmp_path),
        as_of=AS_OF,
        mode=SearchMode.HYBRID,
    )


async def _invocations(conn: psycopg.AsyncConnection[Any]) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM llm_invocation ORDER BY attempt")
        return list(await cur.fetchall())


async def test_supported_citation_passes_and_is_recorded(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """실재하는 인용은 통과하고, 그 호출이 llm_invocation 에 한 행으로 남는다."""
    paragraph_id = await _seed_corpus(owner_conn)
    llm = StubLLM([_payload(str(paragraph_id), "침해사고를 인지한 즉시")])

    outcome = await _run(owner_conn, tmp_path, llm, switches_on)

    assert outcome.status is GateStatus.OK
    assert outcome.gate.citation_count == 1
    assert outcome.attempts == 1

    rows = await _invocations(owner_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "OK"
    assert row["purpose"] == "OBLIGATION_EXTRACTION"
    assert row["model_id"] == "stub:model"
    assert row["api_version"] == "test-version"
    assert row["prompt_template_id"] == "obligation-extraction"
    assert len(row["prompt_template_sha256"]) == 64
    assert row["retrieved_chunk_ids"] == [paragraph_id]
    assert row["retrieval_mode"] == "HYBRID"
    assert row["input_tokens"] == 100
    assert row["s3_key_raw_output"], "원본 출력 위치가 없으면 감사에서 제시할 것이 없다"
    assert "sampling" in row["request_params_json"]


async def test_raw_output_is_stored_and_hashed(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """원본 출력이 저장소에 실제로 있고 해시가 그것과 일치한다 (ADR-007)."""
    import hashlib

    paragraph_id = await _seed_corpus(owner_conn)
    await _run(
        owner_conn,
        tmp_path,
        StubLLM([_payload(str(paragraph_id), "침해사고를 인지한 즉시")]),
        switches_on,
    )

    row = (await _invocations(owner_conn))[0]
    body = await LocalDocumentStore(tmp_path).get(row["s3_key_raw_output"])
    assert hashlib.sha256(body).hexdigest() == row["raw_output_sha256"]


async def test_invented_citation_is_discarded_and_becomes_insufficient(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """검색되지 않은 문단 ID 를 인용하면 폐기되고 결과는 INSUFFICIENT_EVIDENCE 다.

    **모델이 status=OK 라고 말해도 그렇다.** 판정은 코드가 한다.
    """
    await _seed_corpus(owner_conn)
    llm = StubLLM([_payload(str(uuid4()), "지어낸 인용문")])

    outcome = await _run(owner_conn, tmp_path, llm, switches_on)

    assert outcome.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert [d.reason for d in outcome.gate.discarded] == [DiscardReason.NOT_RETRIEVED]
    assert outcome.gate.searched_scope == ("ISP-PROC-002 v2.4",)
    # 호출 자체는 성공했으므로 OK 로 기록된다 — 폐기는 gate 의 판정이지 호출의 실패가 아니다.
    assert (await _invocations(owner_conn))[0]["outcome"] == "OK"


async def test_real_id_with_invented_quote_is_discarded(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """실재 ID + 지어낸 인용문. ID 만 대조하는 구현이 통과시키는 조합이다."""
    paragraph_id = await _seed_corpus(owner_conn)
    outcome = await _run(
        owner_conn,
        tmp_path,
        StubLLM([_payload(str(paragraph_id), "24시간 이내에 신고한다")]),
        switches_on,
    )
    assert outcome.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert [d.reason for d in outcome.gate.discarded] == [DiscardReason.QUOTE_NOT_FOUND]


async def test_schema_violation_retries_once_and_records_both_attempts(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """스키마 위반은 1회 재시도하고, **두 시도가 각각 행으로 남는다**.

    한 행으로 합치면 재작성 비율(ADR-013 신호 2)을 셀 수 없다.
    """
    paragraph_id = await _seed_corpus(owner_conn)
    llm = StubLLM(
        [
            SchemaViolationError("응답이 JSON 이 아니다"),
            _payload(str(paragraph_id), "침해사고를 인지한 즉시"),
        ]
    )

    outcome = await _run(owner_conn, tmp_path, llm, switches_on)

    assert outcome.status is GateStatus.OK
    assert outcome.attempts == 2
    rows = await _invocations(owner_conn)
    assert [r["attempt"] for r in rows] == [1, 2]
    assert [r["outcome"] for r in rows] == ["SCHEMA_INVALID", "OK"]
    assert rows[0]["error_detail"]


async def test_two_schema_violations_end_as_insufficient(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """두 번 실패하면 답을 만들어내지 않고 이관한다. 세 번째를 시도하지 않는다."""
    await _seed_corpus(owner_conn)
    llm = StubLLM([SchemaViolationError("깨진 JSON"), SchemaViolationError("또 깨짐")])

    outcome = await _run(owner_conn, tmp_path, llm, switches_on)

    assert outcome.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert llm.calls == 2
    assert len(await _invocations(owner_conn)) == 2


async def test_no_matching_provision_keeps_the_obligation(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """대응 조항이 없다고 모델이 말하면 주장은 유지되고 상태만 INSUFFICIENT_EVIDENCE 다.

    골든셋 case-013 의 형태 — 새 의무는 실재하고 담을 조항이 없는 것이다.
    이것을 제거하면 담당자가 새 의무를 영영 보지 못한다.
    """
    await _seed_corpus(owner_conn)
    outcome = await _run(
        owner_conn,
        tmp_path,
        StubLLM([_payload(None, "", status="INSUFFICIENT_EVIDENCE")]),
        switches_on,
    )

    assert outcome.status is GateStatus.INSUFFICIENT_EVIDENCE
    assert len(outcome.gate.unsupported) == 1
    assert not outcome.gate.removed
    assert outcome.gate.suggested_action.value == "NEW_PROVISION_REVIEW"


async def test_external_text_stays_out_of_the_system_prompt(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """개정 조문은 user 메시지에만 들어간다. 지침과 데이터가 같은 자리에 있으면 격리가 아니다."""
    paragraph_id = await _seed_corpus(owner_conn)
    llm = StubLLM([_payload(str(paragraph_id), "침해사고를 인지한 즉시")])

    await _run(owner_conn, tmp_path, llm, switches_on)

    assert ARTICLE.after_text not in llm.last_system
    assert ARTICLE.after_text in llm.last_user_content
    assert "<<<EXTERNAL_DATA_BEGIN>>>" in llm.last_user_content


async def test_invocation_rows_cannot_be_altered(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, switches_on: SwitchGate
) -> None:
    """호출 기록은 사건이다. 사후에 고칠 수 있으면 근거가 아니라 주장이 된다."""
    paragraph_id = await _seed_corpus(owner_conn)
    await _run(
        owner_conn,
        tmp_path,
        StubLLM([_payload(str(paragraph_id), "침해사고를 인지한 즉시")]),
        switches_on,
    )

    async with owner_conn.cursor() as cur:
        with pytest.raises(psycopg.DatabaseError) as caught:
            await cur.execute("UPDATE llm_invocation SET outcome = 'ERROR'")
        assert "UPDATE/DELETE" in str(caught.value)
    await owner_conn.rollback()
