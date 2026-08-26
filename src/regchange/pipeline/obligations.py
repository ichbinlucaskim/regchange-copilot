"""개정 조문 한 건을 의무사항 + 근거로 바꾸는 경로 — 검색 → 추출 → gate → 기록.

목적:
    개정 조문 하나를 받아 (a) 사내 규정 후보를 검색하고, (b) 의무사항을 추출하고,
    (c) 근거 강제 gate 를 통과시키고, (d) 호출 전부를 `llm_invocation` 에 남긴다.

구현 이유:
    **LangGraph 노드를 만들지 않는다** (4단계). 지금 그래프를 만들면 승인 `interrupt` 와
    상태 정의가 함께 들어오는데, 그 둘은 아직 검증할 대상이 없다. 대신 **같은 순서를
    순수한 함수 호출로 고정**해 두어, 4단계에서 노드가 이 함수들을 감싸기만 하면 되게 한다.

    **검색을 먼저 한다.** 추출 프롬프트가 후보 문단을 받아야 인용할 대상이 생긴다.
    후보 없이 추출하면 모델이 자기가 아는 조항을 지어내고, gate 는 그것을 전부 폐기하며,
    결과는 항상 `INSUFFICIENT_EVIDENCE` 가 된다 — 그러면 gate 가 작동하는지 아닌지를
    구별할 수 없다.

    **재시도를 여기서 한다.** 스키마 위반 시 1회 재시도 후 실패 처리다(3단계 §5).
    어댑터가 아니라 여기서 하는 이유: 시도마다 `llm_invocation` 행이 하나씩 남아야
    재작성 비율(ADR-013 신호 2)을 셀 수 있다. 어댑터가 조용히 재시도하면 한 행만 남는다.

    **최종 상태를 gate 가 정한다.** 모델의 `status` 는 참고 신호이며, `INSUFFICIENT_EVIDENCE`
    는 통과한 근거가 0건일 때 코드가 붙인다 (`guards/citations.py`).

    **기록 실패는 결과 실패다.** 기록할 수 없으면 결과도 반환하지 않는다. 기록 없는 출력이
    하류로 흐르면 그 출력의 근거를 영원히 알 수 없다.

트레이드오프:
    - 한 조문씩 처리한다. 여러 조문을 묶으면 프롬프트가 길어지고 어느 의무가 어느 조문에서
      나왔는지 흐려진다. 처리량은 호출을 병렬로 돌려 얻는다 — 그것은 이 함수의 관심사가
      아니다 (ADR-013: 작업 큐의 동시성과 LLM 패턴을 섞지 않는다).
    - 검색과 추출이 한 트랜잭션 밖에 있다. 검색은 읽기 전용이고 추출은 외부 호출이므로
      트랜잭션으로 묶을 것이 없다. 기록만 트랜잭션 안에서 한다.
    - **킬 스위치를 진입에서 검사한다 (5단계).** `LLM_ENABLED` 를 이 함수가, `RETRIEVAL_ENABLED`
      를 `search()` 가 본다. 판정 로직은 `guards.killswitch` 한 곳이고 여기서는 부르기만 한다 —
      "검사 지점이 두 곳이 된다"는 우려는 **판정이 두 벌이 되는 것**을 가리켰고, 그것은
      일어나지 않았다. 호출 지점은 오히려 경로마다 있어야 새 진입점이 조용히 빠지지 않는다.

엣지 케이스:
    - **검색 0건**: 추출을 건너뛰지 않는다. 모델에게 "후보가 0건"임을 명시해 전달하고,
      의무사항 자체는 뽑게 한다 — 골든셋 case-013 처럼 "새 의무가 걸리는데 담을 조항이
      없다"가 정답인 경우가 있고, 그 판정에는 의무사항 목록이 필요하다.
      단 인용은 반드시 0건이 되고 결과는 `INSUFFICIENT_EVIDENCE` 다.
    - **스키마 위반 2회**: `INSUFFICIENT_EVIDENCE` 로 끝낸다. 세 번째를 시도하지 않는다 —
      두 번 실패한 프롬프트가 세 번째에 성공하는 것은 개선이 아니라 우연이다.
    - **모델 거부(refusal)**: 재시도하지 않는다. 같은 입력에 같은 판단이 나올 가능성이
      높고, 재시도는 비용만 쓴다. `ERROR` 가 아니라 `REFUSAL` 로 기록한다.
    - **호출 자체 실패(네트워크·크레딧·인증 등)**: `ERROR` 로 기록하고 **예외를 전파한다.**
      조용한 기본값을 돌려주지 않는다. 2026-08-22 까지 코드가 이 약속을 어기고 있었다 —
      `INSUFFICIENT_EVIDENCE` 로 바꿔 정상 반환했고, 그래서 크레딧 소진이 "EMPTY 이관
      3/3(100%)" 으로 보고됐다 (`docs/incidents/measurement-reported-failure-as-success.md`).
    - 인용이 전부 폐기됨: 주장이 제거되고 폐기 사유가 결과에 남는다. 결과가 비었다고
      "영향 없음"으로 읽지 않는다 — `INSUFFICIENT_EVIDENCE` 는 "모른다"이지 "없다"가 아니다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from regchange.adapters.embedding import EmbeddingClient
from regchange.adapters.llm import LLMClient, LLMError, SchemaViolationError
from regchange.adapters.storage import DocumentStore
from regchange.audit.invocation import (
    InvocationOutcome,
    InvocationPurpose,
    InvocationRecord,
    record_invocation,
)
from regchange.guards.citations import GateResult, GateStatus, enforce_citations
from regchange.guards.killswitch import Switch, SwitchGate
from regchange.prompts.obligation import (
    OBLIGATION_SCHEMA,
    PROMPT,
    ExtractionStatus,
    ObligationExtraction,
    SuggestedAction,
    build_user_content,
    parse_extraction,
)
from regchange.retrieval.models import RetrievalResult, SearchMode
from regchange.retrieval.query import build_query
from regchange.retrieval.search import search

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
"""스키마 위반 시 총 시도 횟수. 최초 1회 + 재시도 1회 (3단계 §5, ADR-013).

3회로 늘리지 않는 이유: 두 번 실패한 프롬프트가 세 번째에 성공하는 것은 개선이 아니라
우연이고, 우연에 기대는 경로는 비율이 나빠져도 드러나지 않는다."""

DEFAULT_TOP_K = 10
"""검색 후보 수. `docs/10-retrieval-evaluation-protocol.md` §3 의 결정 지표 k 와 같은 값이다.

측정에 쓴 k 와 운영에 쓰는 k 가 다르면 측정 결과가 운영을 설명하지 못한다."""


@dataclass(frozen=True, slots=True)
class AmendedArticle:
    """추출 입력이 되는 개정 조문 하나.

    `before_text` 가 None 일 수 있다 — 신설 조문이거나 직전 버전을 확보하지 못한 경우다
    (골든셋 3건). 없다는 사실을 프롬프트가 명시적으로 알린다.
    """

    law_name: str
    article_path: str
    revision_kind: str
    change_type: str
    after_text: str
    before_text: str | None = None
    document_versions: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ObligationOutcome:
    """한 조문 처리의 결과 전체. 실패도 결과다."""

    gate: GateResult
    retrieval: RetrievalResult
    invocation_ids: tuple[UUID, ...]
    attempts: int
    injection_signals: tuple[str, ...]
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    cache_read_tokens_total: int = 0
    cache_creation_tokens_total: int = 0
    """이 조문 처리에 쓴 토큰 합계 (재시도 포함).

    **단일 진실은 `llm_invocation` 이다.** 여기 있는 값은 호출부가 다시 질의하지 않아도
    되도록 실어 나르는 사본이며, 비용을 주장할 때는 테이블을 집계한다 — 사본을 근거로
    쓰면 사본이 틀렸을 때 드러나지 않는다."""

    @property
    def status(self) -> GateStatus:
        """최종 상태. gate 가 정한 값이며 모델의 주장이 아니다."""
        return self.gate.status


async def extract_obligations(
    conn: psycopg.AsyncConnection[Any],
    *,
    switches: SwitchGate,
    article: AmendedArticle,
    llm: LLMClient,
    embedding: EmbeddingClient,
    store: DocumentStore,
    as_of: dt.date,
    top_k: int = DEFAULT_TOP_K,
    mode: SearchMode = SearchMode.HYBRID,
    retrieval: RetrievalResult | None = None,
) -> ObligationOutcome:
    """개정 조문 하나를 의무사항과 근거로 바꾼다.

    목적:
        3단계의 ①②와 gate 를 한 경로로 실행하고, 그 과정을 전부 기록한다.

    구현 이유:
        `conn` 을 인자로 받는다. 이 함수는 읽기(검색)와 쓰기(`llm_invocation`)를 모두
        하지만, **쓰기는 `app_graph` 가 INSERT 할 수 있는 테이블로 제한된다** (원칙 5).
        커넥션을 만들지 않고 받는 이유는 어느 role 로 붙을지를 호출부가 정해야 하기
        때문이다 — 여기서 만들면 이 함수가 role 을 고르게 된다.

        **`retrieval` 을 주입할 수 있다 (4단계).** 그래프는 검색을 별도 노드로 두므로
        (`graph/nodes.py` 의 `retrieve_policy`), 이미 수행한 검색 결과를 그대로 넘긴다.
        여기서 다시 검색하면 같은 실행 안에서 **두 개의 서로 다른 후보 집합**이 생기고,
        인용 검증의 정답 집합이 어느 쪽인지 모호해진다. 위임 승격(R-22)이 붙으면서
        그 위험이 실재하게 됐다 — 승격은 검색 밖에서 후보를 더하는 조작이므로, 재검색은
        승격분을 잃는다.

    트레이드오프:
        인자가 많다. 각각이 교체 가능한 경계(LLM·임베딩·저장소)이며, 기본값을 두면
        도메인 코드가 구체 구현을 알게 된다 (ADR-010).

    엣지 케이스:
        모듈 docstring 참조.
        - `retrieval` 이 주어지면 `top_k`·`mode`·`as_of` 는 검색에 쓰이지 않는다.
          주어진 결과가 어떤 조건으로 만들어졌는지는 그 결과 자신이 들고 있다.
    """
    await switches.require(Switch.LLM)
    if retrieval is None:
        query = build_query(
            law_name=article.law_name,
            article_path=article.article_path,
            after_text=article.after_text,
        )
        retrieval = await search(
            conn,
            switches=switches,
            query=query,
            mode=mode,
            limit=top_k,
            as_of=as_of,
            client=embedding,
        )
    candidates = [
        (str(chunk.paragraph_id), f"{chunk.doc_id} {chunk.spec}", chunk.text_raw)
        for chunk in retrieval.chunks
    ]
    if not candidates:
        logger.warning("검색 후보가 0건이다 — 인용은 반드시 0건이 된다 (as_of=%s)", retrieval.as_of)

    user_content, wrap_signals = build_user_content(
        law_name=article.law_name,
        article_path=article.article_path,
        revision_kind=article.revision_kind,
        change_type=article.change_type,
        before_text=article.before_text,
        after_text=article.after_text,
        candidates=candidates,
    )
    # 조립이 끝난 문자열을 다시 훑지 않는다. 스캔은 `wrap_external` 안에서 외부 블록에만
    # 적용된다 (R-23 ②) — 여기서 훑으면 등급 구별이 다시 버려진다.
    signals = tuple(sorted(set(wrap_signals)))
    if signals:
        logger.warning("입력에서 지시 유도 패턴 신호: %s", ", ".join(signals))

    chunk_ids = [chunk.paragraph_id for chunk in retrieval.chunks]
    invocation_ids: list[UUID] = []
    extraction: ObligationExtraction | None = None
    attempt = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0

    last_outcome: InvocationOutcome | None = None
    last_error: str | None = None
    while attempt < MAX_ATTEMPTS and extraction is None:
        attempt += 1
        outcome, parsed, record = await _attempt(
            llm=llm,
            user_content=user_content,
            attempt=attempt,
            chunk_ids=chunk_ids,
            retrieval_mode=retrieval.mode.value,
            document_versions=article.document_versions or {},
            signals=signals,
        )
        async with conn.transaction():
            invocation_ids.append(await record_invocation(conn, store, record))
        # 기록을 즉시 커밋한다. psycopg 는 이미 트랜잭션이 열려 있으면 `transaction()` 을
        # 세이브포인트로 만들므로, 커밋하지 않으면 커넥션이 닫힐 때까지 기록이 보이지 않는다.
        # 다른 프로세스(검토 화면)가 같은 사실을 봐야 하고, 프로세스가 죽어도 남아야 한다.
        await conn.commit()
        input_tokens += record.input_tokens or 0
        output_tokens += record.output_tokens or 0
        cache_read += record.cache_read_input_tokens or 0
        cache_creation += record.cache_creation_input_tokens or 0
        last_outcome, last_error = outcome, record.error_detail
        if outcome is InvocationOutcome.OK:
            extraction = parsed
        elif outcome is not InvocationOutcome.SCHEMA_INVALID:
            # 거부와 오류는 재시도하지 않는다. 같은 입력에 같은 결과가 나온다.
            break

    # **호출 자체가 실패했으면 예외를 전파한다** (2026-08-22 정정, 사건 기록 참조).
    # 그전에는 이 자리에서 `INSUFFICIENT_EVIDENCE` 를 만들어 정상 반환했다. 그러면
    # "모델을 부르지도 못했다"가 "근거를 찾았는데 부족하다"와 같은 값이 되고, 담당자와
    # 측정 러너가 둘을 구별할 수 없다 — 이 모듈 docstring 이 처음부터 금지한 것이다.
    # 거부(REFUSAL)와 스키마 위반은 **모델의 응답**이므로 그대로 이관한다.
    if extraction is None and last_outcome is InvocationOutcome.ERROR:
        msg = f"의무사항 추출 호출이 실패했다 ({attempt}회 시도): {last_error}"
        raise LLMError(msg)

    if extraction is None:
        extraction = ObligationExtraction(
            status=ExtractionStatus.INSUFFICIENT_EVIDENCE,
            obligations=(),
            reason=(
                f"모델 응답이 {attempt}회 시도에서 스키마를 만족하지 않았거나 "
                "호출이 실패했다. 답을 만들어내지 않고 이관한다"
            ),
            suggested_action=SuggestedAction.NONE,
        )

    gate = enforce_citations(
        extraction,
        retrieved={str(chunk.paragraph_id): chunk.text_raw for chunk in retrieval.chunks},
        searched_scope=retrieval.searched_scope,
    )
    if gate.discarded:
        logger.warning(
            "인용 %d건 폐기: %s",
            len(gate.discarded),
            ", ".join(sorted({d.reason.value for d in gate.discarded})),
        )

    return ObligationOutcome(
        gate=gate,
        retrieval=retrieval,
        invocation_ids=tuple(invocation_ids),
        attempts=attempt,
        injection_signals=signals,
        input_tokens_total=input_tokens,
        output_tokens_total=output_tokens,
        cache_read_tokens_total=cache_read,
        cache_creation_tokens_total=cache_creation,
    )


async def _attempt(
    *,
    llm: LLMClient,
    user_content: str,
    attempt: int,
    chunk_ids: list[UUID],
    retrieval_mode: str,
    document_versions: dict[str, str],
    signals: tuple[str, ...],
) -> tuple[InvocationOutcome, ObligationExtraction | None, InvocationRecord]:
    """호출 1회를 시도하고 (결과, 파싱값, 기록할 행)을 돌려준다.

    목적:
        성공·스키마 위반·거부·오류를 같은 형태로 다뤄, **어느 경우에도 기록이 남게** 한다.

    구현 이유:
        예외를 여기서 잡아 `InvocationRecord` 로 변환한다. 위로 던지면 기록 없이 실패하고,
        기록 없는 실패는 "호출하지 않은 것"과 구별되지 않는다 (ADR-014 와 같은 판단).
        단 **삼키지는 않는다** — 결과 값으로 실패를 알리고 호출부가 판단한다.

    트레이드오프:
        반환 튜플이 세 개라 읽기 불편하다. 기록을 빠뜨릴 수 없게 만드는 것이 목적이다.

    엣지 케이스:
        - `SchemaViolationError`: 원본 출력이 없을 수 있다(파싱 이전 실패). 그 경우
          `raw_output` 이 None 이고 DB CHECK 는 `outcome != 'OK'` 이므로 통과한다.
        - 파싱은 됐지만 enum 밖의 값이거나 모양이 다름: `ValueError`/`TypeError`/`KeyError`
          를 전부 스키마 위반으로 취급한다. 결과가 같기 때문이다 — 쓸 수 없는 출력이다.
          다만 예외 이름을 `error_detail` 에 남겨 원인을 구별할 수 있게 한다.
    """
    base = {
        "purpose": InvocationPurpose.OBLIGATION_EXTRACTION,
        "prompt_template_id": PROMPT.id,
        "prompt_template_sha256": PROMPT.sha256,
        "model_id": llm.model_id,
        "attempt": attempt,
        "input_document_versions": document_versions,
        "retrieved_chunk_ids": chunk_ids,
        "retrieval_mode": retrieval_mode,
        "injection_signals": signals,
    }

    try:
        result = await llm.complete(
            prompt_id=PROMPT.id,
            prompt_version=PROMPT.version,
            system=PROMPT.system,
            user_content=user_content,
            response_schema=OBLIGATION_SCHEMA,
        )
    except SchemaViolationError as exc:
        return (
            InvocationOutcome.SCHEMA_INVALID,
            None,
            InvocationRecord(
                api_version="unknown",
                request_params={"note": "스키마 위반으로 응답을 폐기했다"},
                outcome=InvocationOutcome.SCHEMA_INVALID,
                error_detail=str(exc),
                **base,  # type: ignore[arg-type]
            ),
        )
    except LLMError as exc:
        outcome = (
            InvocationOutcome.REFUSAL
            if type(exc).__name__ == "RefusalError"
            else InvocationOutcome.ERROR
        )
        return (
            outcome,
            None,
            InvocationRecord(
                api_version="unknown",
                request_params={"note": "호출이 실패했다"},
                outcome=outcome,
                error_detail=str(exc),
                **base,  # type: ignore[arg-type]
            ),
        )

    try:
        parsed = parse_extraction(result.output)
    except (ValueError, TypeError, KeyError) as exc:
        return (
            InvocationOutcome.SCHEMA_INVALID,
            None,
            InvocationRecord(
                api_version=result.api_version,
                request_params=result.request_params,
                outcome=InvocationOutcome.SCHEMA_INVALID,
                raw_output=result.raw_text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_input_tokens=result.cache_read_input_tokens,
                cache_creation_input_tokens=result.cache_creation_input_tokens,
                latency_ms=result.latency_ms,
                error_detail=f"{type(exc).__name__}: {exc}",
                **base,  # type: ignore[arg-type]
            ),
        )

    return (
        InvocationOutcome.OK,
        parsed,
        InvocationRecord(
            api_version=result.api_version,
            request_params=result.request_params,
            outcome=InvocationOutcome.OK,
            raw_output=result.raw_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_input_tokens=result.cache_read_input_tokens,
            cache_creation_input_tokens=result.cache_creation_input_tokens,
            latency_ms=result.latency_ms,
            **base,  # type: ignore[arg-type]
        ),
    )
