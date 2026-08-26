"""Anthropic Messages API 구현 — 구조화 출력만 쓰고, 보낸 것을 그대로 기록한다.

목적:
    `LLMClient` 계약을 Anthropic API 로 만족한다. 기본 모델은 `claude-sonnet-5` 이고,
    낮게 나온 케이스만 `claude-opus-5` 로 다시 돌려 "모델 한계인가 프롬프트 한계인가"를
    가른다.

구현 이유:
    **`output_config.format` (json_schema) 를 쓴다.** 도구 호출을 강제하는 방식이나
    "JSON 으로만 답하라"는 프롬프트 지시가 아니라 API 수준의 형식 강제다. 프롬프트
    지시는 지켜지지 않을 수 있고, 지켜지지 않았을 때 파싱 실패로만 드러난다.
    형식 강제는 gate 1단(구조 강제)을 API 경계에서 미리 통과시킨다.

    **`temperature` / `top_p` / `seed` 를 보내지 않는다.** 대상 모델이 이 파라미터들을
    거부한다(HTTP 400). 샘플링 제어 대신 `output_config.effort` 로 사고 깊이를 정한다.
    경위는 `docs/incidents/llm-api-schema-drift.md` (미수 3) — 이 사실을 확인하지 않고
    스키마를 그렸다가 구현 직전에 잡았다.

    **`request_params` 를 우리가 실제로 보낸 본문에서 만든다.** 호출부가 따로 재구성하면
    기록과 실제가 달라질 수 있고, 그 차이는 아무 오류도 내지 않는다.

    **재시도를 이 계층에서 하지 않는다.** SDK 의 자동 재시도(네트워크·429·5xx)는 그대로
    두되, **스키마 위반 재시도는 파이프라인이 한다.** 어댑터가 조용히 다시 부르면
    `llm_invocation` 에 시도가 한 행으로만 남고 재작성 비율(ADR-013 신호 2)을 셀 수 없다.

    **시스템 지침에 프롬프트 캐시 브레이크포인트를 건다** (2026-08-24). 캐싱은 접두부
    재사용이고 접두부는 `tools → system → messages` 순으로 쌓인다. 우리는 도구를 쓰지
    않으므로 정적인 부분은 `system` 하나뿐이고, 동적인 것(개정 조문·검색 문단)은 전부
    `messages` 에 있다. **순서를 바꿀 것이 없다** — 프롬프트가 이미 캐싱이 요구하는
    모양이었다. 자세한 근거는 `SYSTEM_CACHE_CONTROL` 과 `MIN_CACHEABLE_PROMPT_TOKENS`.

트레이드오프:
    - Anthropic SDK 에 결합된다. 공급자를 바꾸면 이 파일을 다시 쓴다. 도메인 코드는
      `LLMClient` 만 보므로 파급이 여기서 멈춘다 (ADR-010).
    - 스트리밍을 쓰지 않는다. 구조화 출력은 전체가 모여야 파싱되고, 이 시스템에는 검증
      전 출력을 사용자에게 보낼 경로가 없다. 대신 긴 출력에서 HTTP 타임아웃 위험이
      있으므로 `max_tokens` 를 필요한 만큼만 둔다.
    - 응답 검증을 `jsonschema` 라이브러리가 아니라 최소 검사로 한다. 의존성을 하나
      늘리는 대신, API 가 이미 형식을 강제하므로 남는 위험은 "필수 키 누락"뿐이고
      그것만 확인한다. **이 판단은 `output_config.format` 이 실제로 스키마를 강제한다는
      전제에 의존하며, 그 전제가 깨지면 gate 2단이 뒤에서 잡는다.**

엣지 케이스:
    - **API 키 없음**: `LLMConfigError`. 빈 키로 호출해 401 을 받는 대신 호출 전에
      실패한다 — 401 은 키가 없는 것인지 틀린 것인지 구별하지 못한다.
      키는 `LLM_API_KEY` → `ANTHROPIC_API_KEY` 순으로 찾는다 (`API_KEY_ENVS` 참조).
    - **거부(`stop_reason == "refusal"`)**: 예외가 아니라 결과로 돌려준다. 거부는 오류가
      아니라 사실이며 `llm_invocation` 에 남아야 한다. 다만 `output` 이 없으므로
      `SchemaViolationError` 가 아니라 `RefusalError` 로 구별해 올린다.
    - **`max_tokens` 도달(`stop_reason == "max_tokens"`)**: 잘린 JSON 은 파싱되지 않으므로
      `SchemaViolationError` 가 된다. 잘렸다는 사실을 메시지에 담아 재시도 판단을 돕는다.
    - **응답에 text 블록이 없음**: `SchemaViolationError`. 조용히 빈 dict 를 돌려주지 않는다.
    - **빈 스키마**: 호출하지 않고 `ValueError`.
    - **캐시 접두부가 최소 토큰에 미달**: 오류가 아니라 **조용한 무효과**다. 공식 문서가
      "No error is returned" 라고 적는다. 실제로 `citation-grounding`(1,005 토큰)이
      Sonnet 5 의 하한 1,024 에 19 토큰 모자라 캐시되지 않는다 —
      `MIN_CACHEABLE_PROMPT_TOKENS` 참조. 이 사실은 `cache_read_input_tokens` 가 0으로
      남는 것으로만 드러나므로 **비용 집계가 그 값을 반드시 읽어야 한다.**
    - **네트워크·API 오류**: SDK 의 `APIError` 를 `LLMError` 로 옮겨 던진다. 옮기지 않으면
      호출부의 실패 처리 경로가 발화하지 않아 **실패한 호출이 기록되지 않는다.** 실측으로
      드러났다 — 4단계 골든셋 측정 중 네트워크 오류 한 번이 기록 없이 실행 전체를
      중단시켰다 (`docs/12-impact-assessment-results.md` §8).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from regchange.adapters.llm.base import (
    JsonSchema,
    LLMError,
    LLMResult,
    SchemaViolationError,
    StructuredResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
"""기본 모델. 구조화 출력과 한국어 법령 해석 품질의 균형점으로 골랐다.

낮게 나온 케이스만 `claude-opus-5` 로 다시 돌린다 — 두 모델 모두에서 실패하면 프롬프트나
검색의 한계이고, Opus 에서만 성공하면 모델 한계다. **대조가 진단이 된다.**"""

COMPARISON_MODEL = "claude-opus-5"
"""실패 케이스 대조용 모델. 기본값으로 쓰지 않는다 — 반복 측정 비용이 누적된다."""

API_KEY_ENVS = ("LLM_API_KEY", "ANTHROPIC_API_KEY")
"""API 키를 찾는 환경변수 순서. **`LLM_API_KEY` 가 먼저다.**

저장소 관례가 공급자 중립 이름이다 — `.env` 가 `LLM_PROVIDER`/`LLM_MODEL` 과 짝을 이루는
`LLM_API_KEY` 를 쓰고, ADR-010 이 공급자를 교체 가능한 것으로 둔다. 설정 파일에
`ANTHROPIC_` 접두사가 박히면 공급자 교체가 설정 변경까지 끌고 간다.

`ANTHROPIC_API_KEY` 를 뒤에 두는 이유는 SDK 의 기본 이름이기 때문이다. 그 변수를 이미
export 해 둔 환경에서 이유 없이 실패하지 않게 한다 — 다만 우리는 키를 명시적으로
넘기므로 SDK 의 자동 탐색에 기대지 않는다."""

ANTHROPIC_API_VERSION = "2023-06-01"
"""`anthropic-version` 헤더 값. SDK 가 보내는 값과 같으며 `llm_invocation.api_version` 에
기록한다. 같은 모델 ID 라도 API 버전이 바뀌면 기본값과 응답 형태가 바뀔 수 있다."""

MAX_OUTPUT_TOKENS = 16000
"""출력 토큰 상한. **실측으로 올렸다 (2026-08-21, 8000 → 16000).**

처음 8000 으로 둔 근거는 의무사항 추출이었다 — "조문 하나에서 나오는 의무사항은 많아야
십수 건이고 각 항목이 짧다". 그 근거가 4단계에서 깨졌다.

**영향평가 초안은 추출보다 크다.** 필드가 많고(영향 문단·통제항목·부서 근거·증빙),
`thinking` 이 출력 토큰에 계상된다. 골든셋 측정에서 성공한 초안이 **7,848 출력 토큰**이었고
그 다음 케이스가 상한에서 잘려 `SCHEMA_INVALID` 두 번으로 이관됐다. 잘림은 파싱 실패로
시끄럽게 드러났지만 — 설계대로다 — 그 케이스는 **모델 능력이 아니라 상한 때문에** 근거
부족이 됐고, 그 상태로 측정하면 지표가 우리 상수를 재게 된다.

16000 은 관측된 성공 출력의 약 2배이고, 비스트리밍 요청의 권장 기본값이기도 하다
(대상 모델은 128K 까지 가능하지만 그 크기는 스트리밍을 요구한다). 상한을 더 키우지 않는
이유는 처음과 같다: 폭주한 출력의 비용은 조용히 커진다."""

REQUEST_TIMEOUT_SECONDS = 120.0
"""호출 타임아웃. 사고(thinking)가 관여하므로 단일 호출이 수십 초까지 갈 수 있다.
무한 대기를 만들지 않는 것이 목적이며, 이 값을 넘으면 실패로 기록된다."""

EFFORT = "high"
"""사고 깊이. `low`~`max` 중 `high` 를 쓴다.

`temperature` 가 없는 API 표면에서 품질을 조절하는 축이 이것뿐이다. 규제 문언 해석은
정확도가 처리량보다 중요하므로 기본값(`high`)을 낮추지 않는다. 비용이 문제가 되면
낮추기 전에 **낮췄을 때의 골든셋 점수를 먼저 잰다.**"""

MIN_CACHEABLE_PROMPT_TOKENS = 1024
"""프롬프트 캐시가 듣는 최소 접두부 토큰 수 — **모델별 값이며 Sonnet 5 기준이다.**

공식 문서(`prompt-caching.md`, 2026-08-24 확인)가 모델별로 512 / 1,024 / 2,048 / 4,096 을
정한다. 기본 모델(`claude-sonnet-5`)과 대조 모델(`claude-opus-5`)의 값이 다르므로
(각각 1,024 / 512) 여기 적은 값은 **더 엄격한 쪽**이다.

이 값이 상수로 존재하는 이유는 분기하기 위해서가 아니라 **측정된 사실을 코드 옆에
남기기 위해서다.** 시스템 지침 4종을 `count_tokens` 로 실측한 결과(2026-08-24):

    obligation-extraction  1,326 토큰   ≥ 1,024  → 캐시된다
    impact-assessment      1,963 토큰   ≥ 1,024  → 캐시된다
    citation-grounding     1,005 토큰   < 1,024  → **19 토큰이 모자라 캐시되지 않는다**
    citation-blind           930 토큰   < 1,024  → 캐시되지 않는다

**호출 수가 가장 많은 프롬프트가 캐시되지 않는다.** 그것을 채우려면 시스템 지침에
문장을 더해야 하고, 그러면 프롬프트가 바뀌어 기준선이 깨진다. 지금 하지 않는다 —
`docs/17-engineering-knobs.md` §5. 미달은 오류가 아니라 **조용한 무효과**이므로
(문서: "No error is returned") 이 사실은 `cache_read_input_tokens` 로만 드러난다."""

SYSTEM_CACHE_CONTROL: Mapping[str, object] = {"type": "ephemeral"}
"""시스템 지침 블록에 거는 캐시 브레이크포인트. TTL 은 기본값(5분)이다.

**최상위 `cache_control`(자동 캐싱)을 쓰지 않는다.** 그것은 *마지막* 캐시 가능 블록에
브레이크포인트를 놓는데, 우리 요청에서 마지막 블록은 케이스마다 달라지는 사용자
메시지다. 그러면 매 호출이 캐시 **쓰기**(1.25배)가 되고 읽기는 한 번도 일어나지 않아
**비용이 늘어난다.** 정적인 것이 앞, 동적인 것이 뒤라는 접두부 규칙이 지켜지는 자리는
`system` 하나뿐이므로 브레이크포인트도 거기 하나만 둔다.

TTL 을 1시간(2배 쓰기)으로 올리지 않는 이유: 실측상 같은 프롬프트의 연속 호출 간격이
최대 208초였다(2026-08-22 골든셋 15건 실행). 5분 안에 들어오므로 더 비싼 쓰기를 살
이유가 없다. **케이스당 호출 간격이 5분을 넘기 시작하면 이 판단을 다시 한다.**"""


class LLMConfigError(LLMError):
    """클라이언트를 구성할 수 없다. 호출 전에 실패한다."""


class RefusalError(LLMError):
    """모델이 안전상의 이유로 응답을 거부했다. 오류가 아니라 사실로 기록한다."""


class ClaudeClient:
    """Anthropic Messages API 로 구조화 출력을 얻는다.

    목적:
        `LLMClient` 계약을 만족하고, 호출 사실을 `LLMResult` 로 함께 돌려준다.

    구현 이유:
        API 키를 생성자에서 확인한다. 호출 시점에 확인하면 파이프라인이 검색까지 마친 뒤에
        실패하고, 그 실패가 "검색 결과가 없다"처럼 보이는 지점에서 발생한다.

        `anthropic` SDK 를 함수 안에서 import 한다. 수집·파싱·차분·검색 경로는 LLM 을
        쓰지 않으며, 그 경로들이 SDK 로딩 비용을 낼 이유가 없다.

    트레이드오프:
        비동기 클라이언트를 인스턴스마다 하나 만든다. 수명이 짧은 배치 작업이므로
        커넥션 점유가 문제되지 않는다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        effort: str = EFFORT,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        """API 키를 호출 전에 확인한다. 파이프라인 중간에서 실패하는 경로를 만들지 않는다."""
        key = api_key or next(
            (v for name in API_KEY_ENVS if (v := os.environ.get(name, "")).strip()), ""
        )
        if not key.strip():
            msg = (
                f"{' / '.join(API_KEY_ENVS)} 가 모두 비어 있다. 호출 전에 실패한다 — "
                "빈 키로 진행하면 실패가 인증 오류로 위장한다"
            )
            raise LLMConfigError(msg)
        self._model = model
        self._key = key
        self._effort = effort
        self._max_output_tokens = max_output_tokens
        self._client: Any | None = None

    @property
    def model_id(self) -> str:
        """`llm_invocation.model_id` 에 기록되는 값."""
        return self._model

    @property
    def api_version(self) -> str:
        """`llm_invocation.api_version` 에 기록되는 값."""
        return ANTHROPIC_API_VERSION

    def _sdk(self) -> Any:
        """SDK 클라이언트를 한 번만 만든다. `Any` 인 이유는 외부 경계이기 때문이다."""
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._key, timeout=REQUEST_TIMEOUT_SECONDS)
        return self._client

    def _request_params(self, *, prompt_id: str, prompt_version: str) -> dict[str, object]:
        """실제로 보낸 파라미터를 그대로 만든다. `request_params_json` 이 이 값을 받는다.

        샘플링 파라미터가 없다는 사실 자체를 기록한다 — 나중에 "왜 temperature 가
        없는가"를 묻는 사람에게 답이 되어야 한다.
        """
        return {
            "model": self._model,
            "max_tokens": self._max_output_tokens,
            "output_config": {"effort": self._effort, "format": "json_schema"},
            "thinking": "adaptive",
            "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "sampling": "unsupported_by_model",
            # 캐시 브레이크포인트를 어디에 걸었는지 기록한다. 캐시 히트 여부는
            # `cache_read_input_tokens` 가 말하지만, **어디에 걸었는지는 응답이
            # 말해 주지 않는다.** 히트가 0인 실행을 나중에 볼 때 "안 걸었다"와
            # "걸었는데 안 들었다"를 이 필드가 가른다.
            "cache_control": {"scope": "system", **SYSTEM_CACHE_CONTROL},
        }

    async def complete(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        system: str,
        user_content: str,
        response_schema: JsonSchema,
    ) -> LLMResult:
        """프롬프트를 실행하고 스키마를 만족하는 구조화 응답과 호출 사실을 반환한다.

        목적:
            `llm_invocation` 한 행을 만들기에 충분한 결과를 돌려준다.

        구현 이유:
            외부 텍스트를 `user_content` 로만 받는다. 시스템 지침과 같은 메시지에 합치면
            지시와 데이터의 경계가 사라지고, 격리를 지켰는지 사후에 확인할 수 없다
            (기획서 10.1).

        트레이드오프:
            지연을 `time.perf_counter` 로 잰다. 네트워크 왕복과 SDK 재시도가 모두 포함되어
            모델 자체의 지연보다 크게 나온다. 그 편이 맞다 — 우리가 실제로 기다린 시간이다.

        엣지 케이스:
            모듈 docstring 참조.
        """
        if not response_schema:
            msg = "빈 스키마로 호출할 수 없다. 스키마 없는 호출은 정의되지 않은 사용이다"
            raise ValueError(msg)

        from anthropic import APIError

        params = self._request_params(prompt_id=prompt_id, prompt_version=prompt_version)
        started = time.perf_counter()
        try:
            response = await self._sdk().messages.create(
                model=self._model,
                max_tokens=self._max_output_tokens,
                # 블록 형태로 보내는 것은 캐시 브레이크포인트를 걸기 위해서다.
                # **텍스트는 한 글자도 바뀌지 않는다** — 문자열 하나를 같은 내용의
                # text 블록 하나로 감쌌을 뿐이고, 모델이 받는 프롬프트는 동일하다.
                system=[
                    {"type": "text", "text": system, "cache_control": dict(SYSTEM_CACHE_CONTROL)}
                ],
                messages=[{"role": "user", "content": user_content}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": dict(response_schema)},
                },
                thinking={"type": "adaptive"},
            )
        except APIError as exc:
            # SDK 예외를 우리 계약(`LLMError`)으로 옮긴다. 옮기지 않으면 호출부의 실패
            # 처리 경로가 발화하지 않고, **실패한 호출이 `llm_invocation` 에 남지 않는다** —
            # "실패한 호출"과 "하지 않은 호출"이 같은 부재가 된다 (마이그레이션 010).
            # 실측으로 드러난 문제다: 골든셋 측정 중 네트워크 오류 한 번이 파이프라인의
            # 기록 없이 실행 전체를 중단시켰다 (`docs/12-impact-assessment-results.md` §8).
            msg = f"모델 호출이 실패했다: {type(exc).__name__}: {exc}"
            raise LLMError(msg) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        stop_reason = str(getattr(response, "stop_reason", "") or "")
        usage = getattr(response, "usage", None)
        raw_text = _first_text(response)

        if stop_reason == "refusal":
            detail = getattr(getattr(response, "stop_details", None), "category", None)
            msg = f"모델이 응답을 거부했다 (category={detail})"
            raise RefusalError(msg)

        result = LLMResult(
            output=_parse(raw_text, response_schema, stop_reason=stop_reason),
            raw_text=raw_text,
            model_id=self._model,
            api_version=ANTHROPIC_API_VERSION,
            request_params=params,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
            input_tokens=_usage(usage, "input_tokens"),
            output_tokens=_usage(usage, "output_tokens"),
            cache_read_input_tokens=_usage(usage, "cache_read_input_tokens"),
            cache_creation_input_tokens=_usage(usage, "cache_creation_input_tokens"),
        )
        logger.info(
            "llm 호출: model=%s prompt=%s@%s in=%s out=%s %dms",
            self._model,
            prompt_id,
            prompt_version,
            result.input_tokens,
            result.output_tokens,
            latency_ms,
        )
        return result


def _usage(usage: object, field: str) -> int | None:
    """토큰 수를 읽되 없으면 None. 0으로 채우면 비용 집계가 조용히 틀린다."""
    value = getattr(usage, field, None)
    return int(value) if isinstance(value, int) else None


def _first_text(response: object) -> str:
    """응답에서 첫 text 블록을 꺼낸다. 없으면 빈 문자열."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    return ""


def _parse(raw_text: str, schema: JsonSchema, *, stop_reason: str) -> StructuredResponse:
    """원본 텍스트를 파싱하고 필수 키 존재만 확인한다.

    목적:
        gate 1단(구조 강제)의 마지막 확인. API 가 형식을 강제하지만 그 전제가 깨졌을 때
        조용히 통과시키지 않는다.

    구현 이유:
        스키마 밖 필드를 여기서 버린다. 파싱 단계에서 버려야 하류가 그 필드를 보고
        분기하는 코드를 만들지 못한다 (3단계 §5 "스키마 밖 필드는 파싱 단계에서 폐기").

    트레이드오프:
        전체 JSON Schema 검증(타입·형식·중첩)을 하지 않는다. 의존성을 하나 아낀 대신,
        API 의 형식 강제를 신뢰한다. 신뢰가 깨지면 필수 키 검사와 gate 2단이 잡는다.

    엣지 케이스:
        - 빈 문자열: `SchemaViolationError`.
        - 잘린 JSON(`max_tokens`): 파싱 실패에 그 사실을 담는다. 재시도 판단의 재료다.
        - 최상위가 객체가 아님: `SchemaViolationError`.
    """
    if not raw_text.strip():
        msg = f"응답에 텍스트가 없다 (stop_reason={stop_reason})"
        raise SchemaViolationError(msg)
    try:
        loaded: Any = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        truncated = " — 출력이 max_tokens 에서 잘렸다" if stop_reason == "max_tokens" else ""
        msg = f"응답이 JSON 이 아니다{truncated}: {exc}"
        raise SchemaViolationError(msg) from exc

    if not isinstance(loaded, Mapping):
        msg = f"응답 최상위가 객체가 아니다: {type(loaded).__name__}"
        raise SchemaViolationError(msg)

    properties = schema.get("properties")
    allowed = set(properties) if isinstance(properties, Mapping) else set(loaded)
    required = schema.get("required")
    missing = [key for key in required if key not in loaded] if isinstance(required, list) else []
    if missing:
        msg = f"응답에 필수 필드가 없다: {missing}"
        raise SchemaViolationError(msg)

    # 스키마 밖 필드는 여기서 버린다. 하류가 볼 수 없으면 그것에 의존할 수도 없다.
    return {key: value for key, value in loaded.items() if key in allowed}
