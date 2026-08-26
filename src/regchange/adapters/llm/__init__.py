"""LLM 호출 경계 — 도메인 코드는 이 Protocol 만 본다 (ADR-010).

목적:
    모델 호출을 공급자와 무관한 하나의 계약으로 노출한다.

구현 이유:
    구현체(`claude`)를 여기서 재수출하지 않는다. `adapters/storage` 가 Protocol 만
    재수출하고 구체 구현을 하위 모듈에 둔 것과 같다. 재수출하면 도메인 코드가
    `from regchange.adapters.llm import ClaudeClient` 를 쓰게 되고, import-linter
    계약이 잡기 전까지 그것이 가장 편한 길이 된다.

    조립은 설정 코드(파이프라인 진입점, 평가 러너)가 한다. 어느 모델을 쓸지는
    **운영 결정**이며 도메인 로직이 알 일이 아니다 — 이 저장소는 기본을 Sonnet 5 로
    두고 낮게 나온 케이스만 Opus 5 로 대조하기로 했고, 그 대조가 성립하려면 모델이
    호출부에서 갈아 끼워지는 값이어야 한다.

트레이드오프:
    호출부가 구현 모듈 경로를 알아야 해서 import 가 길어진다. 그 마찰이 경계를 지키게 한다.

엣지 케이스:
    - `dispatch` 는 이 패키지를 import 할 수 없다 (원칙 5, import-linter 계약).
      발송 워커가 프롬프트나 모델 출력을 보는 경로를 만들지 않는다.
"""

from regchange.adapters.llm.base import (
    JsonSchema,
    LLMClient,
    LLMError,
    LLMResult,
    SchemaViolationError,
    StructuredResponse,
)

__all__ = [
    "JsonSchema",
    "LLMClient",
    "LLMError",
    "LLMResult",
    "SchemaViolationError",
    "StructuredResponse",
]
