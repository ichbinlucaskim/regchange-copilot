"""gate 3단 검증기 구현 2종 — anchored 와 de-anchored. **호출마다 행을 남긴다** (원칙 3).

목적:
    `SupportVerifier` 계약을 Anthropic 호출로 만족한다. 두 구현체를 **동시에 유지**해
    같은 골든셋으로 대조할 수 있게 한다. 판정 하나가 `llm_invocation` 한 행 이상이며,
    실패한 판정도 행으로 남는다.

구현 이유:
    **두 구현체를 남기는 것이 이 모듈의 요점이다.**

    | 구현체 | 검증기가 보는 것 | 판정 어휘 |
    |---|---|---|
    | `RecordingSupportVerifier` (anchored) | 주장 + 인용문 | `SupportLevel` (정도) |
    | `DeAnchoredSupportVerifier` | ①·② 두 호출 (아래) | `ClaimRelation` (관계) |

    de-anchored 의 두 호출이 보는 것:
      - ① 개정 조문 + 인용 문단 — **초안은 보지 않는다**
      - ② ①의 출력 + 인용 문단 + 초안의 주장

    앞의 것이 4단계에서 case-013 을 놓친 구현이고, 뒤의 것이 그 원인(후보를 보고 판정)을
    고친 구현이다. **한쪽을 지우면 대조가 불가능해진다** — 지금 지우지 않는 이유는
    ADR-016 이 `VECTOR`/`LEXICAL` 경로를 남긴 것과 같다.

    **`verify()` 서명이 둘 다 같다.** 개정 조문은 de-anchored 구현체가 **생성자에서**
    받는다. 개정 조문은 평가 한 건 동안 바뀌지 않는 값이고, 서명을 갈라 두면 호출부가
    구현체를 알게 되어 대조가 "같은 자리에서 갈아 끼우기"가 아니게 된다.

    **블라인드 결과를 문단 단위로 캐시한다.** 같은 문단을 여러 주장이 인용하면 1단계를
    다시 부르지 않는다. 캐시 히트 수를 세어 두는 이유는 **비용 계산에 필요하기 때문**이며,
    세지 않으면 "호출이 늘었다"만 남고 얼마나 늘었는지는 남지 않는다.

    **`verification/` 이 아니라 `pipeline/` 에 둔다.** 검증 판정은 도메인이지만 모델 호출은
    I/O 다. `verification` 패키지는 I/O 를 갖지 않는다는 계약이 import-linter 로 걸려 있고,
    그 계약이 있어야 "생성기가 자기 안에서 검증하는" 경로가 구조적으로 만들어지지 않는다.

    **실패는 통과가 아니다.** 호출이 실패하면 `ERROR` 로 기록하고 예외를 전파한다.
    조용히 `SUPPORTED`/`WITHIN` 을 돌려주는 구현은 계약 위반이며, 그런 구현은 gate 가 있는
    것처럼 보이면서 없는 상태를 만든다.

트레이드오프:
    - 판정마다 DB 쓰기가 일어난다. 묶어서 쓰면 빨라지지만, 중간에 죽었을 때 어느 판정이
      실제로 일어났는지 알 수 없게 된다. 기록의 완결성이 처리량보다 중요하다.
    - 스키마 위반 시 재시도하지 않는다. 판정 스키마는 필드가 둘뿐이라 위반이 일어나면
      모델이 형식을 못 지키는 상태이고, 그 상태에서 한 번 더 부르는 것은 우연에 기대는 것이다.
    - `effort` 를 생성기와 같은 값으로 둔다. 검증은 사고가 짧을 것 같지만 그것은 추측이고,
      낮췄을 때의 판정 품질을 재기 전에는 근거 없는 상수를 하나 더 만드는 일이다.
    - de-anchored 는 캐시가 있어도 호출이 는다. **줄어드는지가 아니라 무엇을 잡는지가
      판단 기준**이며, 그것은 대조 측정이 답한다.

엣지 케이스:
    - **빈 주장·빈 인용문·빈 개정 조문**: 호출 전에 `ValueError`.
    - **모델 거부(refusal)**: `REFUSAL` 로 기록하고 예외를 전파한다. 거부는 판정이 아니다.
    - **스키마 위반**: `SCHEMA_INVALID` 로 기록하고 예외를 전파한다.
    - **판정값이 enum 밖**: 파싱에서 `ValueError`. 알 수 없는 판정을 통과로 떨어뜨리지 않는다.
    - **1단계가 빈 기준을 냄**: `ValueError`. 빈 기준은 무엇이든 통과시킨다
      (`prompts/deanchored.py`).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import psycopg

from regchange.adapters.llm import JsonSchema, LLMClient, LLMError, LLMResult, SchemaViolationError
from regchange.adapters.storage import DocumentStore
from regchange.audit.invocation import (
    InvocationOutcome,
    InvocationPurpose,
    InvocationRecord,
    record_invocation,
)
from regchange.prompts.deanchored import (
    BLIND_PROMPT,
    BLIND_SCHEMA,
    CONTRAST_PROMPT,
    CONTRAST_SCHEMA,
    build_blind_content,
    build_contrast_content,
    parse_blind,
    parse_contrast,
)
from regchange.prompts.grounding import (
    GROUNDING_SCHEMA,
    PROMPT,
    build_user_content,
    parse_verdict,
)
from regchange.prompts.models import PromptTemplate
from regchange.verification.base import RELATION_TO_LEVEL, SupportVerdict

logger = logging.getLogger(__name__)


class _RecordingVerifier:
    """호출과 기록을 공유하는 뼈대. 두 검증기가 같은 기록 경로를 쓴다.

    목적:
        "어느 경우에도 행이 남는다"를 한 곳에서 보장한다.

    구현 이유:
        기록 코드를 구현체마다 두면 한쪽만 갱신되는 일이 생기고, 그때 **기록이 빠진 쪽은
        조용히 빠진다.** 성공·실패·파싱 실패를 한 함수가 다룬다.

    트레이드오프:
        상속을 쓴다. 이 저장소는 합성을 선호하지만, 여기서 나누는 것은 상태(커넥션·누적
        카운터)와 그 상태를 쓰는 절차이며 합성으로 옮기면 인자만 늘어난다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    def __init__(
        self,
        *,
        conn: psycopg.AsyncConnection[Any],
        llm: LLMClient,
        store: DocumentStore,
        impact_assessment_id: UUID,
        chunk_ids: list[UUID],
        document_versions: dict[str, str],
        revision: int = 0,
    ) -> None:
        """평가 한 건에 대한 검증기를 만든다. **초안은 받지 않는다** (원칙 3)."""
        self._conn = conn
        self._llm = llm
        self._store = store
        self._assessment_id = impact_assessment_id
        self._chunk_ids = chunk_ids
        self._document_versions = document_versions
        self.revision = revision
        self.invocation_ids: list[UUID] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        """캐시에서 읽은 입력 토큰. **`input_tokens` 에 포함되지 않는다** — API 가
        브레이크포인트 이후의 토큰만 `input_tokens` 로 준다. 따로 세지 않으면 비용이
        조용히 작아진다 (프롬프트 캐싱 도입, 2026-08-24)."""
        self.cache_creation_tokens = 0
        """캐시에 쓴 입력 토큰. 읽기보다 비싸다(1.25배). 읽기와 합치지 않는 이유는
        단가가 다르고, **히트율을 보려면 둘을 나눠 세야 하기 때문**이다."""

    async def _invoke(
        self,
        *,
        purpose: InvocationPurpose,
        template: PromptTemplate,
        schema: JsonSchema,
        user_content: str,
        signals: tuple[str, ...],
    ) -> tuple[LLMResult, dict[str, Any]]:
        """호출 1회를 수행하고 `(결과, 기록할 값)` 을 돌려준다.

        기록할 값을 함께 돌려주는 이유는 **파싱 성공 여부에 따라 같은 값으로 다른 행을
        남겨야 하기 때문**이다. 호출부가 다시 조립하면 어느 한쪽이 빠질 수 있다.
        실패하면 여기서 행을 남기고 예외를 전파한다 — 실패는 통과가 아니다.
        """
        base: dict[str, Any] = {
            "purpose": purpose,
            "prompt_template_id": template.id,
            "prompt_template_sha256": template.sha256,
            "model_id": self._llm.model_id,
            "revision": self.revision,
            "impact_assessment_id": self._assessment_id,
            "input_document_versions": self._document_versions,
            "retrieved_chunk_ids": self._chunk_ids,
            "injection_signals": signals,
        }

        try:
            result = await self._llm.complete(
                prompt_id=template.id,
                prompt_version=template.version,
                system=template.system,
                user_content=user_content,
                response_schema=schema,
            )
        except LLMError as exc:
            outcome = (
                InvocationOutcome.SCHEMA_INVALID
                if isinstance(exc, SchemaViolationError)
                else InvocationOutcome.REFUSAL
                if type(exc).__name__ == "RefusalError"
                else InvocationOutcome.ERROR
            )
            await self._record(
                InvocationRecord(
                    api_version="unknown",
                    request_params={"note": "검증 호출이 실패했다"},
                    outcome=outcome,
                    error_detail=str(exc),
                    **base,
                )
            )
            # 실패는 통과가 아니다. 호출부가 폐기하도록 전파한다.
            raise

        return result, base

    async def _accept(self, result: LLMResult, base: dict[str, Any]) -> None:
        """파싱까지 성공한 호출을 `OK` 로 기록한다."""
        await self._record(
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
                **base,
            )
        )
        self.input_tokens += result.input_tokens or 0
        self.output_tokens += result.output_tokens or 0
        self.cache_read_tokens += result.cache_read_input_tokens or 0
        self.cache_creation_tokens += result.cache_creation_input_tokens or 0

    async def _reject(self, result: LLMResult, base: dict[str, Any], exc: Exception) -> None:
        """파싱에 실패한 호출을 `SCHEMA_INVALID` 로 기록한다. 원본 출력은 남긴다."""
        await self._record(
            InvocationRecord(
                api_version=result.api_version,
                request_params=result.request_params,
                outcome=InvocationOutcome.SCHEMA_INVALID,
                raw_output=result.raw_text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                # 실패한 시도의 캐시 토큰도 청구된다 — 빼면 비용 기록이 낙관적이 된다.
                cache_read_input_tokens=result.cache_read_input_tokens,
                cache_creation_input_tokens=result.cache_creation_input_tokens,
                latency_ms=result.latency_ms,
                error_detail=f"{type(exc).__name__}: {exc}",
                **base,
            )
        )

    async def _record(self, record: InvocationRecord) -> None:
        """판정 호출 한 건을 기록한다. 기록 실패는 예외로 전파된다."""
        async with self._conn.transaction():
            self.invocation_ids.append(await record_invocation(self._conn, self._store, record))
        # 판정 하나가 끝나면 그 기록은 커밋되어 있어야 한다. 커밋하지 않으면 커넥션이
        # 닫힐 때까지 보이지 않고, 프로세스가 죽으면 "검증했다"는 주장만 남는다.
        await self._conn.commit()


class RecordingSupportVerifier(_RecordingVerifier):
    """**anchored** — 주장과 인용문을 함께 보고 뒷받침 정도를 판정한다.

    목적:
        4단계에서 만든 원래 구현. de-anchored 와의 대조를 위해 남아 있다.

    구현 이유:
        입력이 주장·인용문·문단 표기뿐이다. 개정 조문도 받지 않는다 — 이 구현의 설계가
        "검증기 입력을 최소로 둔다"였기 때문이며, **그 판단이 case-013 을 놓친 원인의
        일부**라는 것이 4단계 측정에서 드러났다 (`docs/12-impact-assessment-results.md` §5).

    트레이드오프:
        **후보를 보고 판정한다.** 그래서 주장이 약해지면 판정이 위로 움직인다. 이 성질을
        고치지 않고 남겨 둔 이유는 대조 기준선이 필요하기 때문이다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    async def verify(self, *, claim: str, quote: str, spec: str) -> SupportVerdict:
        """인용문이 주장을 뒷받침하는지 판정한다. 문장을 고쳐 쓰지 않는다."""
        user_content, signals = build_user_content(claim=claim, quote=quote, spec=spec)
        result, base = await self._invoke(
            purpose=InvocationPurpose.CITATION_VERIFICATION,
            template=PROMPT,
            schema=GROUNDING_SCHEMA,
            user_content=user_content,
            signals=signals,
        )
        try:
            level, reason = parse_verdict(result.output)
        except (ValueError, TypeError, KeyError) as exc:
            await self._reject(result, base, exc)
            msg = f"검증 판정을 읽을 수 없다: {exc}"
            raise SchemaViolationError(msg) from exc

        await self._accept(result, base)
        return SupportVerdict(level=level, reason=reason)


class DeAnchoredSupportVerifier(_RecordingVerifier):
    """**de-anchored** — 자기 답을 먼저 내고, 그 답을 기준으로 초안의 주장을 대조한다.

    목적:
        후보를 보고 판정하는 구조를 없앤다. 판정 기준이 초안이 아니라 **검증기가 먼저 낸
        자기 답**이 된다.

    구현 이유:
        `amendment` 를 생성자에서 받는다. 평가 한 건 동안 바뀌지 않는 값이고, `verify()`
        서명을 anchored 와 같게 유지해 호출부가 구현체를 갈아 끼우기만 하면 되게 한다.

        **의무사항 추출 결과는 받지 않는다.** 개정 조문은 외부 입력이지만 의무사항은 우리
        모델이 해석한 것이다. 경계는 "외부 입력인가, 우리 출력인가"이며 그 경계를 넘으면
        검증기가 생성기의 판단을 물려받는다.

        블라인드 결과를 `문단 표기 → (말할 수 있는 것, 말할 수 없는 것)` 으로 캐시한다.
        캐시 키를 문단 ID 가 아니라 표기로 두는 이유는 이 클래스가 ID 를 받지 않기
        때문이다 — ID 를 받으면 검증기가 그것을 근거로 삼을 여지가 생긴다.

    트레이드오프:
        호출이 (문단당 1회) + (주장당 1회)로 는다. 캐시가 문단 재사용을 흡수하지만
        anchored 보다 항상 많다. 그 대가로 얻는 것이 무엇인지는 측정이 답한다.

        1단계가 틀리면 2단계도 틀린다. 검증기가 문단을 잘못 읽으면 정당한 주장이
        `BEYOND` 가 되며, 기존 구조에서는 초안이 그 오류를 교정할 여지가 있었다.
        **그 여지를 일부러 없앤 것이다.**

    엣지 케이스:
        모듈 docstring 참조.
    """

    def __init__(self, *, amendment: str, **kwargs: Any) -> None:
        """개정 조문을 함께 받는다. 초안·의무사항은 받지 않는다."""
        super().__init__(**kwargs)
        if not amendment.strip():
            msg = "개정 조문 없이는 블라인드 단계의 방향이 없다"
            raise ValueError(msg)
        self._amendment = amendment
        self._blind: dict[str, tuple[str, str]] = {}
        self.blind_calls = 0
        self.blind_cache_hits = 0

    async def _blind_for(self, *, quote: str, spec: str) -> tuple[str, str]:
        """이 문단으로 말할 수 있는 것과 없는 것. 문단 단위로 캐시한다."""
        cached = self._blind.get(spec)
        if cached is not None:
            self.blind_cache_hits += 1
            return cached

        user_content, signals = build_blind_content(
            amendment=self._amendment, quote=quote, spec=spec
        )
        result, base = await self._invoke(
            purpose=InvocationPurpose.CITATION_BLIND,
            template=BLIND_PROMPT,
            schema=BLIND_SCHEMA,
            user_content=user_content,
            signals=signals,
        )
        try:
            claimable, limits = parse_blind(result.output)
        except (ValueError, TypeError, KeyError) as exc:
            await self._reject(result, base, exc)
            msg = f"블라인드 단계 출력을 읽을 수 없다: {exc}"
            raise SchemaViolationError(msg) from exc

        await self._accept(result, base)
        self.blind_calls += 1
        self._blind[spec] = (claimable, limits)
        return claimable, limits

    async def verify(self, *, claim: str, quote: str, spec: str) -> SupportVerdict:
        """자기 답을 먼저 내고, 그 답과 초안의 주장의 **관계**를 판정한다.

        1단계 출력을 2단계 프롬프트에 **그대로** 싣는다. 요약하거나 다듬으면 그 과정에서
        초안 쪽으로 기울 수 있다.
        """
        claimable, limits = await self._blind_for(quote=quote, spec=spec)

        user_content, signals = build_contrast_content(
            claimable=claimable, limits=limits, claim=claim, quote=quote, spec=spec
        )
        result, base = await self._invoke(
            purpose=InvocationPurpose.CITATION_CONTRAST,
            template=CONTRAST_PROMPT,
            schema=CONTRAST_SCHEMA,
            user_content=user_content,
            signals=signals,
        )
        try:
            relation, reason = parse_contrast(result.output)
        except (ValueError, TypeError, KeyError) as exc:
            await self._reject(result, base, exc)
            msg = f"대조 판정을 읽을 수 없다: {exc}"
            raise SchemaViolationError(msg) from exc

        await self._accept(result, base)
        return SupportVerdict(level=RELATION_TO_LEVEL[relation], reason=reason, relation=relation)
