"""`llm_invocation` 기록 — **첫 호출부터** 남긴다.

목적:
    모든 모델 호출에 대해 무엇을 보냈고 무엇을 받았는지를 DB 한 행과 저장소 한 객체로
    남긴다. 성공·실패·폐기를 가리지 않는다.

구현 이유:
    **ADR-013 이 LangGraph 를 쓰는 조건으로 이 기록을 걸었다** — "프레임워크가 실제로
    무엇을 보냈는지 우리가 항상 볼 수 있어야 한다". 나중에 붙이면 초기 호출들의 이력이
    없고, 그러면 그 조건이 거짓이 된다. 그래서 그래프보다 이것을 먼저 만든다.

    **원본 출력을 저장소에, 해시를 DB 에 둔다** (ADR-007 과 같은 형태). 모델 출력은 크고,
    DB 에 넣으면 조회가 무거워지며 백업 비용이 는다. 해시가 DB 에 있으므로 파일이 바뀌면
    드러난다 — 스냅샷 페이지를 sha256 으로 검증하는 것과 같은 발상이다.

    **실패한 호출도 행으로 남긴다.** `outcome` 이 그것을 구별한다. 실패를 기록하지 않으면
    "실패한 호출"과 "하지 않은 호출"이 같은 부재가 되고, 그 둘은 조치가 다르다 — `ops_run`
    을 만든 이유와 같다 (ADR-014).

    **재시도를 별도 행으로 남긴다.** `attempt` 로 구분한다. 한 행에 합치면 시도 횟수를
    셀 수 없다.

    **재작성을 `revision` 으로 따로 센다.** 스키마 위반 재시도(`attempt`)와 검증 실패로
    인한 재작성(`revision`)은 원인도 조치도 다르다. 한 컬럼에 섞으면 ADR-013 의 「틀렸음을
    알게 되는 신호 2번」(재작성 비율이 높다 → 프롬프트나 검색을 의심하라)이 재시도율과
    뒤섞여 신호가 되지 못한다.

    **`app_graph` role 로 쓴다.** 이 role 이 INSERT 할 수 있는 테이블은
    `llm_invocation` 과 `audit_event` 둘뿐이다 (원칙 5). 기록을 위해 더 넓은 권한을
    요구하지 않도록 스키마를 그렇게 짰다.

트레이드오프:
    - 저장소 쓰기와 DB 쓰기가 두 단계라 사이에서 죽으면 **객체는 있는데 행이 없는** 상태가
      생긴다. 반대 순서(행 먼저)로 하면 **행은 있는데 객체가 없는** 상태가 되고, 그쪽이
      더 나쁘다 — 기록이 가리키는 원본이 없으면 감사에서 제시할 것이 없다. 고아 객체는
      용량만 차지한다. 그래서 **저장소를 먼저 쓴다.**
    - 기록 실패를 삼키지 않는다. 기록할 수 없으면 호출 결과도 쓰지 않는다. 기록 없는
      출력이 하류로 흘러가면 그 출력의 근거를 영원히 알 수 없다. 규제 도메인에서
      "기록은 실패했지만 결과는 썼다"는 허용되지 않는다.
    - 토큰 수가 없으면 NULL 로 둔다. 0 으로 채우면 비용 집계가 조용히 틀린다.

엣지 케이스:
    - **`temperature`/`seed` 를 기록하지 않는다.** 대상 모델이 그 파라미터를 거부하므로
      기록할 값이 존재하지 않는다. `request_params_json` 에 실제로 보낸 것만 담는다.
      경위는 `docs/incidents/llm-api-schema-drift.md` (미수 3).
    - 검색 결과가 0건: `retrieved_chunk_ids` 가 빈 배열이다. 정상이며 gate 가 판정할
      입력이다.
    - `outcome='OK'` 인데 출력이 없는 경우: DB CHECK 제약이 막는다. 조용한 빈 성공을
      허용하지 않는다.
    - 같은 호출을 두 번 기록: `id` 가 다르므로 두 행이 된다. 멱등을 만들지 않는 이유는
      **호출이 실제로 두 번 일어났을 가능성**을 지울 수 없기 때문이다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from regchange.adapters.storage import DocumentStore

logger = logging.getLogger(__name__)

RAW_OUTPUT_PREFIX = "llm-output"
"""원본 출력 객체 키의 접두사. `llm-output/<날짜>/<호출 id>.json` 형태다.

날짜를 넣는 이유는 객체가 쌓였을 때 사람이 훑을 수 있어야 하기 때문이고, 호출 id 를
쓰는 이유는 DB 행에서 객체로 바로 갈 수 있어야 하기 때문이다."""


class InvocationPurpose(StrEnum):
    """어떤 단계의 호출인가. 생성기와 검증기를 구별한다 (원칙 3)."""

    OBLIGATION_EXTRACTION = "OBLIGATION_EXTRACTION"
    """의무사항 추출 (3단계)."""
    IMPACT_ASSESSMENT = "IMPACT_ASSESSMENT"
    """영향평가 초안 생성 (4단계). evaluator-optimizer 의 생성기 쪽이다."""
    CITATION_VERIFICATION = "CITATION_VERIFICATION"
    """인용 의미 뒷받침 검증 (gate 3단, **anchored**). 초안의 주장과 인용문을 함께 본다.

    이 값과 `IMPACT_ASSESSMENT` 가 같은 `impact_assessment_id` 아래 공존하는지가
    생성기·검증기 분리를 사후에 확인하는 방법이다 — 검증 행이 없는 초안은 검증받지 않은
    초안이다 (원칙 3)."""
    CITATION_BLIND = "CITATION_BLIND"
    """gate 3단 de-anchored 1단계 — **초안을 보지 않고** 문단으로 말할 수 있는 것을 먼저 낸다.

    문단 단위로 캐시되므로 같은 평가 안에서 호출 수가 주장 수보다 적을 수 있다.
    그 차이가 캐시 히트이며, 비용 계산에 필요하다."""
    CITATION_CONTRAST = "CITATION_CONTRAST"
    """gate 3단 de-anchored 2단계 — 1단계 출력과 초안의 주장을 대조해 관계를 판정한다.

    `CITATION_BLIND` 없이 이 행만 있으면 de-anchoring 이 성립하지 않은 것이다 —
    두 행이 짝을 이루는지가 질의로 확인된다."""


class InvocationOutcome(StrEnum):
    """호출 결과. DB CHECK 제약과 같은 값이어야 한다."""

    OK = "OK"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    REFUSAL = "REFUSAL"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """`llm_invocation` 한 행에 들어갈 값 전부.

    호출부가 이 객체를 만들어 넘긴다. 어댑터가 돌려준 `LLMResult` 의 값을 그대로 옮기며,
    옮기는 과정에서 재계산하지 않는다 — 재계산한 기록은 기록이 아니다.
    """

    purpose: InvocationPurpose
    prompt_template_id: str
    prompt_template_sha256: str
    model_id: str
    api_version: str
    request_params: Mapping[str, object]
    outcome: InvocationOutcome
    attempt: int = 1
    revision: int = 0
    """evaluator-optimizer 재작성 회차. 0 이 최초 초안이고 1 이 재작성이다.

    **`attempt` 와 다른 값이다.** `attempt` 는 스키마 위반으로 같은 프롬프트를 다시 부른
    것이고, `revision` 은 검증이 떨어뜨려 **다른 프롬프트로** 다시 쓴 것이다. 한 컬럼에
    섞으면 "재작성 비율"(ADR-013 신호 2)이 재시도율과 뒤섞여 신호가 되지 못한다."""
    impact_assessment_id: UUID | None = None
    """이 호출이 어느 영향평가에 속하는가. 초안·검증·재작성을 한 건으로 묶는 키다."""
    input_document_versions: Mapping[str, object] | None = None
    retrieved_chunk_ids: Sequence[UUID] = ()
    retrieval_mode: str | None = None
    raw_output: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    latency_ms: int | None = None
    error_detail: str | None = None
    injection_signals: Sequence[str] = ()


_INSERT = """
INSERT INTO llm_invocation (
    id, invoked_at, purpose, attempt, revision, impact_assessment_id,
    prompt_template_id, prompt_template_sha256,
    model_id, api_version, request_params_json,
    input_document_versions_json, retrieved_chunk_ids, retrieval_mode,
    raw_output_sha256, s3_key_raw_output,
    input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    latency_ms, outcome, error_detail, injection_signals_json
) VALUES (
    %(id)s, %(invoked_at)s, %(purpose)s, %(attempt)s, %(revision)s, %(impact_assessment_id)s,
    %(prompt_template_id)s, %(prompt_template_sha256)s,
    %(model_id)s, %(api_version)s, %(request_params)s,
    %(input_document_versions)s, %(retrieved_chunk_ids)s, %(retrieval_mode)s,
    %(raw_output_sha256)s, %(s3_key_raw_output)s,
    %(input_tokens)s, %(output_tokens)s, %(cache_read)s, %(cache_creation)s,
    %(latency_ms)s, %(outcome)s, %(error_detail)s, %(injection_signals)s
)
"""


async def record_invocation(
    conn: psycopg.AsyncConnection[Any],
    store: DocumentStore,
    record: InvocationRecord,
    *,
    invoked_at: dt.datetime | None = None,
) -> UUID:
    """호출 하나를 저장소와 DB 에 남기고 그 id 를 반환한다.

    목적:
        "그때 무엇을 보냈고 무엇을 받았는가"에 답할 수 있는 유일한 근거를 만든다.

    구현 이유:
        **저장소를 먼저 쓰고 DB 를 나중에 쓴다.** 사이에서 죽었을 때 남는 상태가 덜
        나쁜 쪽을 골랐다 (모듈 docstring 트레이드오프 참조).

        해시를 여기서 계산한다. 호출부가 계산해 넘기면 원본과 해시가 어긋난 채로 들어올
        수 있고, 그 어긋남은 원본을 다시 해시해 보기 전까지 드러나지 않는다.

    트레이드오프:
        커밋하지 않는다. 호출부의 트랜잭션 경계 안에서 돌기 위해서다 — 파이프라인이
        "기록과 결과를 함께 커밋"할 수 있어야 하며, 여기서 커밋하면 그 선택지가 사라진다.

    엣지 케이스:
        - `raw_output` 이 None 이고 `outcome` 이 OK: DB CHECK 가 거부한다. 여기서 미리
          막지 않는 이유는, 제약을 두 곳에 두면 한쪽이 갱신되지 않았을 때 어긋나기
          때문이다. DB 가 단일 진실이다.
        - 저장소 쓰기 실패: 예외를 전파한다. 기록 없는 결과를 만들지 않는다.
    """
    invocation_id = uuid4()
    stamp = invoked_at or dt.datetime.now(dt.UTC)

    raw_sha256: str | None = None
    object_key: str | None = None
    if record.raw_output is not None:
        body = record.raw_output.encode("utf-8")
        raw_sha256 = hashlib.sha256(body).hexdigest()
        object_key = f"{RAW_OUTPUT_PREFIX}/{stamp:%Y-%m-%d}/{invocation_id}.json"
        await store.put(object_key, body)

    async with conn.cursor() as cursor:
        await cursor.execute(
            _INSERT,
            {
                "id": invocation_id,
                "invoked_at": stamp,
                "purpose": record.purpose.value,
                "attempt": record.attempt,
                "revision": record.revision,
                "impact_assessment_id": record.impact_assessment_id,
                "prompt_template_id": record.prompt_template_id,
                "prompt_template_sha256": record.prompt_template_sha256,
                "model_id": record.model_id,
                "api_version": record.api_version,
                "request_params": Jsonb(dict(record.request_params)),
                "input_document_versions": Jsonb(dict(record.input_document_versions or {})),
                "retrieved_chunk_ids": list(record.retrieved_chunk_ids),
                "retrieval_mode": record.retrieval_mode,
                "raw_output_sha256": raw_sha256,
                "s3_key_raw_output": object_key,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "cache_read": record.cache_read_input_tokens,
                "cache_creation": record.cache_creation_input_tokens,
                "latency_ms": record.latency_ms,
                "outcome": record.outcome.value,
                "error_detail": record.error_detail,
                "injection_signals": Jsonb(list(record.injection_signals)),
            },
        )

    logger.info(
        "llm_invocation 기록: id=%s purpose=%s outcome=%s attempt=%d",
        invocation_id,
        record.purpose.value,
        record.outcome.value,
        record.attempt,
    )
    return invocation_id
