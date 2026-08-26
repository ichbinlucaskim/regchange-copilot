"""호출 실패가 **조용히 결과로 바뀌지 않는가** — 세 겹의 발화 테스트.

이 테스트가 존재하는 이유:
    2026-08-22 에 API 크레딧이 소진됐고, 측정 러너는 **아무 호출도 성공하지 않은 실행을
    「EMPTY 이관 3/3(만점)」으로 보고했다** (`docs/incidents/measurement-reported-failure-
    as-success.md`). 원인은 파이프라인이 호출 실패를 `INSUFFICIENT_EVIDENCE` 로
    바꿔치기한 것이었고, **모듈 docstring 은 처음부터 "예외를 전파한다"고 적혀 있었다.**

    적혀 있는 것과 도는 것이 다르다는 사실은 **발화시켜 보기 전까지 드러나지 않는다.**
    그래서 크레딧을 일부러 떨어뜨리는 대신 호출 실패를 모킹해서 세 겹을 각각 발화시킨다.

    | 겹 | 무엇을 검사하는가 |
    |---|---|
    | 1 | 파이프라인이 예외를 **전파**하는가 (추출·초안 두 자리) |
    | 2 | 러너가 **연속 2회에서 중단**하고 실행을 무효로 표시하는가 |
    | 3 | 집계기가 **무효 실행을 거부**하는가 |

    함께 검사하는 것: **거부(REFUSAL)는 전파하지 않는다.** 그것은 모델의 응답이지
    인프라 실패가 아니다. 과교정하면 "모델이 거부했다"까지 실행을 죽이게 된다.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from evals.runners import impact_eval, variability
from psycopg.rows import dict_row

from regchange.adapters.llm.base import JsonSchema, LLMError, LLMResult
from regchange.adapters.llm.claude import RefusalError
from regchange.adapters.storage.local import LocalDocumentStore
from regchange.guards.killswitch import Switch, SwitchGate
from regchange.ops.switches import set_switch
from regchange.pipeline.impact import GroundingMode, assess_impact, build_context
from regchange.pipeline.obligations import AmendedArticle, extract_obligations
from regchange.retrieval.models import SearchMode

NOW = dt.datetime(2026, 8, 22, 9, 0, tzinfo=dt.UTC)
AS_OF = dt.date(2026, 2, 1)

PARAGRAPH_TEXT = (
    "① 「정보통신망 이용촉진 및 정보보호 등에 관한 법률」 제48조의3에 따라 "
    "침해사고를 인지한 즉시 과학기술정보통신부장관 또는 한국인터넷진흥원에 신고한다."
)

ARTICLE = AmendedArticle(
    law_name="정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    article_path="제48조의3",
    revision_kind="일부개정",
    change_type="MODIFIED",
    after_text="침해사고 발생 사실을 알게 된 때부터 24시간 이내에 신고하여야 한다.",
)

CREDIT_ERROR = (
    "모델 호출이 실패했다: BadRequestError: Error code: 400 - "
    "{'error': {'message': 'Your credit balance is too low to access the Anthropic API.'}}"
)
"""실제로 받은 오류 문자열을 그대로 쓴다. 모킹이 현실과 다른 형태면 시험이 헐거워진다."""


class StubEmbedding:
    """결정론적 벡터. 순위는 이 테스트의 관심사가 아니다."""

    model_id = "stub:embedding"
    dimensions = 4

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(t) for t in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        return (1.0, (sum(ord(c) for c in text) % 97) / 100.0, 0.0, 0.0)


class FailingLLM:
    """지정한 프롬프트에서 실패하는 스텁. 나머지는 정상 응답을 준다."""

    model_id = "stub:failing"

    def __init__(
        self,
        *,
        fail_on: str,
        error: Exception,
        ok_payloads: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._fail_on = fail_on
        self._error = error
        self._ok = ok_payloads or {}
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        prompt_id: str,
        prompt_version: str,  # noqa: ARG002 — Protocol 계약
        system: str,  # noqa: ARG002
        user_content: str,  # noqa: ARG002
        response_schema: JsonSchema,  # noqa: ARG002
    ) -> LLMResult:
        self.calls.append(prompt_id)
        if prompt_id == self._fail_on:
            raise self._error
        payload = self._ok[prompt_id]
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
                    {
                        "paragraph_id": paragraph_id,
                        "quote": "침해사고를 인지한 즉시",
                        "start": 0,
                        "end": 12,
                    }
                ],
            }
        ],
    }


async def _seed_corpus(conn: psycopg.AsyncConnection[Any]) -> UUID:
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


async def _invocations(conn: psycopg.AsyncConnection[Any]) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT purpose, outcome, error_detail FROM llm_invocation")
        return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# 1겹 — 파이프라인이 전파한다
# ---------------------------------------------------------------------------


async def test_extract_obligations_propagates_call_failure(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
    switches_on: SwitchGate,
) -> None:
    """추출 호출이 실패하면 **예외가 나온다.** 근거 부족으로 위장하지 않는다."""
    await _seed_corpus(owner_conn)
    llm = FailingLLM(fail_on="obligation-extraction", error=LLMError(CREDIT_ERROR))

    with pytest.raises(LLMError, match="추출 호출이 실패했다"):
        await extract_obligations(
            owner_conn,
            switches=switches_on,
            article=ARTICLE,
            llm=llm,
            embedding=StubEmbedding(),
            store=LocalDocumentStore(tmp_path),
            as_of=AS_OF,
            mode=SearchMode.HYBRID,
        )

    rows = await _invocations(owner_conn)
    assert [r["outcome"] for r in rows] == ["ERROR"], "실패해도 **행은 남아야** 한다"
    assert "credit balance" in str(rows[0]["error_detail"])


async def test_assess_impact_propagates_draft_call_failure(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
    switches_on: SwitchGate,
) -> None:
    """초안 호출이 실패하면 **예외가 나온다.** 빈 초안이 하류로 흐르지 않는다."""
    paragraph_id = await _seed_corpus(owner_conn)
    llm = FailingLLM(
        fail_on="impact-assessment",
        error=LLMError(CREDIT_ERROR),
        ok_payloads={"obligation-extraction": _obligation(str(paragraph_id))},
    )
    store = LocalDocumentStore(tmp_path)

    obligations = await extract_obligations(
        owner_conn,
        switches=switches_on,
        article=ARTICLE,
        llm=llm,
        embedding=StubEmbedding(),
        store=store,
        as_of=AS_OF,
        mode=SearchMode.HYBRID,
    )
    ctx = build_context(
        law_name=ARTICLE.law_name,
        article_path=ARTICLE.article_path,
        revision_kind=ARTICLE.revision_kind,
        change_type=ARTICLE.change_type,
        after_text=ARTICLE.after_text,
        obligations=obligations.gate,
        retrieval=obligations.retrieval,
    )

    with pytest.raises(LLMError, match="초안 호출이 실패했다"):
        await assess_impact(owner_conn, switches=switches_on, ctx=ctx, llm=llm, store=store)


async def test_refusal_is_not_treated_as_infrastructure_failure(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
    switches_on: SwitchGate,
) -> None:
    """**거부는 전파하지 않는다.** 모델의 응답이지 호출 실패가 아니다.

    과교정을 막는 테스트다. 거부까지 예외로 만들면 "모델이 답하기를 거부했다"가
    실행 전체를 죽이고, 그 사실이 기록이 아니라 스택트레이스로만 남는다.
    """
    await _seed_corpus(owner_conn)
    llm = FailingLLM(fail_on="obligation-extraction", error=RefusalError("모델이 거부했다"))

    outcome = await extract_obligations(
        owner_conn,
        switches=switches_on,
        article=ARTICLE,
        llm=llm,
        embedding=StubEmbedding(),
        store=LocalDocumentStore(tmp_path),
        as_of=AS_OF,
        mode=SearchMode.HYBRID,
    )

    assert outcome.status.value == "INSUFFICIENT_EVIDENCE"
    rows = await _invocations(owner_conn)
    assert [r["outcome"] for r in rows] == ["REFUSAL"], "거부는 ERROR 와 다른 값으로 남는다"


# ---------------------------------------------------------------------------
# 2겹 — 러너가 중단하고 무효로 표시한다
# ---------------------------------------------------------------------------


async def test_impact_eval_aborts_after_two_consecutive_failures(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role_test_dsn: Any,
) -> None:
    """러너가 **연속 2회에서 멈추고** 실행을 `valid=false` 로 표시한다.

    3케이스를 주고 2케이스에서 멈추는지 본다. 크레딧이 없는 상태로 15케이스를 끝까지
    도는 것은 「정상처럼 보이는 결과 파일」을 하나 더 만드는 일일 뿐이다.
    """
    await _seed_corpus(owner_conn)
    for switch in (Switch.RETRIEVAL, Switch.LLM):
        await set_switch(
            owner_conn,
            switch=switch,
            enabled=True,
            changed_by="test",
            reason="러너 발화 테스트",
            now=NOW,
        )

    llm = FailingLLM(fail_on="obligation-extraction", error=LLMError(CREDIT_ERROR))
    monkeypatch.setattr(impact_eval, "ClaudeClient", lambda _model: llm)
    monkeypatch.setattr(impact_eval, "build_embedding", lambda _name: StubEmbedding())
    monkeypatch.setattr(impact_eval, "snapshot_root", lambda: tmp_path)
    monkeypatch.setattr(impact_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(impact_eval, "role_dsn", lambda role: role_test_dsn(role))

    await impact_eval.run(
        "claude-sonnet-5",
        {"case-001", "case-002", "case-003"},
        promote=False,
        mode=GroundingMode.ANCHORED,
    )

    written = list((tmp_path / "results").glob("impact-*.json"))
    assert len(written) == 1
    summary = json.loads(written[0].read_text(encoding="utf-8"))["summary"]

    assert summary["valid"] is False, "실패한 실행이 유효로 표시됐다"
    assert summary["aborted"] is True
    assert len(summary["call_errors"]) == impact_eval.ABORT_AFTER_CONSECUTIVE_ERRORS
    assert summary["cases_measured"] == 0
    assert summary["cases_total"] == 3, "돌지 않은 케이스가 있다는 사실이 남아야 한다"
    assert llm.calls.count("obligation-extraction") == 2, "중단하지 않고 끝까지 돌았다"


# ---------------------------------------------------------------------------
# 3겹 — 집계기가 거부한다
# ---------------------------------------------------------------------------


def _run_file(*, valid: bool, tokens: int, legacy: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "impact_hit_any": 8,
        "empty_correct": 3,
        "grounding": {"SUPPORTED": 7},
        "input_tokens": tokens,
    }
    if not legacy:
        summary["valid"] = valid
        summary["call_errors"] = [] if valid else [{"case_id": "case-007", "error": "x"}]
        summary["aborted"] = not valid
    return {
        "summary": summary,
        "cases": [
            {
                "case_id": "case-001",
                "impact_status": "OK",
                "hit": [],
                "departments": [],
                "cited": [],
            }
        ],
    }


def test_aggregator_refuses_runs_marked_invalid() -> None:
    """`valid=false` 가 섞이면 **편차를 계산하지 않는다.**

    실패한 실행의 수치는 편차가 아니라 결측이다. 섞어서 평균을 내면 "변동성이 크다"는
    잘못된 결론이 나오고, 그 결론은 다음 단계의 설계를 바꾼다.
    """
    runs = [
        _run_file(valid=True, tokens=100_000),
        _run_file(valid=False, tokens=0),
        _run_file(valid=True, tokens=100_000),
    ]

    report = variability.compare_impact(runs)

    assert report["aggregated"] is False
    assert [bad["index"] for bad in report["invalid_runs"]] == [1]
    assert "aggregate" not in report, "무효인데 집계값을 함께 내놓으면 그것이 인용된다"


def test_aggregator_falls_back_to_zero_tokens_for_legacy_files() -> None:
    """`valid` 키가 없는 옛 파일은 **토큰 0** 으로 거른다. 성공한 호출은 토큰을 쓴다."""
    runs = [_run_file(valid=True, tokens=0, legacy=True)]

    report = variability.compare_impact(runs)

    assert report["aggregated"] is False
    assert "토큰 0" in report["invalid_runs"][0]["reason"]


def test_aggregator_still_works_when_every_run_is_valid() -> None:
    """정상 실행만 있으면 집계한다 — 거부가 과하면 아무것도 못 잰다."""
    runs = [_run_file(valid=True, tokens=100_000) for _ in range(3)]

    report = variability.compare_impact(runs)

    assert report.get("aggregated") is not False
    assert report["unstable_count"] == 0
