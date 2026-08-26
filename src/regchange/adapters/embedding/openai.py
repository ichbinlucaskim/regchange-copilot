"""OpenAI 임베딩 구현 — 비교 대상이며, 채택 시 축 2 제약이 따라온다.

목적:
    `text-embedding-3-large` 로 임베딩을 만든다. 로컬 모델과의 품질 비교 대상이다.

구현 이유:
    **이 구현을 만드는 이유는 채택하기 위해서가 아니라 재기 위해서다.** 임베딩 선택은
    되돌리기 비싼 축에 든다 — 바꾸면 코퍼스를 재인덱싱해야 하고, 그 시점에 평가 결과가
    쌓여 있으면 "모델을 바꿔서 좋아진 것인가 다른 것이 바뀐 것인가"를 가를 수 없다.
    이 저장소는 도메인 선택도 diff 채점 기준도 측정으로 정했다. 임베딩만 감으로 정할
    이유가 없다.

    **채택될 경우 축 2 제약이 따라온다.** 사내 규정 원문을 외부 API 로 보내는 것은
    은행에서 실제로 걸리는 지점이며, 개인정보를 포함하지 않더라도 내부 문서의 외부
    전송은 별도 판단을 받는다. 운영 도입 시에는 로컬 모델이나 금융 클라우드 전용
    리전으로 대체해야 하고, `EmbeddingClient` 경계가 그 교체를 흡수한다 (ADR-010).
    이 판단은 코드 주석이 아니라 ADR 에 남는다.

    **`httpx` 를 직접 쓰고 공식 SDK 를 쓰지 않는다.** 이 저장소는 이미 법제처 API 를
    `httpx` 로 호출하며(`ingest/`), 임베딩 엔드포인트는 요청·응답이 단순하다. SDK 를
    넣으면 재시도·타임아웃 정책이 두 벌이 되고, 그 두 벌이 다르게 동작하는 것을
    아무도 눈치채지 못한다.

트레이드오프:
    API 키가 필요하고 네트워크 실패가 검색 실패가 된다. 로컬 구현에는 없는 실패
    모드이며, 이 차이 자체가 채택 판단의 재료다.

    배치를 요청 하나에 최대 `MAX_BATCH` 개까지만 담는다. 더 크게 보내면 요청 실패 시
    다시 보내야 하는 양이 늘고, 작게 보내면 왕복이 는다.

엣지 케이스:
    - API 키 없음: `EmbeddingConfigError`. 빈 키로 호출해 401 을 받는 대신 호출 전에
      실패한다 — 401 은 키가 없는 것인지 틀린 것인지 구별하지 못한다.
    - 빈 문자열: `ValueError`. API 도 거부하지만 호출 전에 막는다.
    - 응답의 `index` 가 요청 순서와 다름: 명세상 순서가 보장되지만 `index` 로 다시
      정렬한다. 순서가 어긋나면 문단과 벡터가 뒤바뀌고, 그 오류는 아무 예외도 내지
      않은 채 검색 결과만 무의미하게 만든다.
    - 응답 차원이 요청과 다름: `ValueError`. DB CHECK 가 다시 잡지만 여기서 먼저 실패한다.
    - HTTP 오류: `httpx` 예외를 그대로 전파한다. 조용한 재시도를 넣지 않는다 —
      측정 중 재시도가 조용히 일어나면 비용 실측이 틀린다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

import httpx

from regchange.adapters.embedding.base import Vector

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_MODEL = "text-embedding-3-large"
DEFAULT_DIMENSIONS = 3072
"""`text-embedding-3-large` 의 기본 차원. 축소 차원을 쓰지 않는 이유는 비교 대상인
로컬 모델을 그 모델의 기본 차원으로 쓰기 때문이다 — 한쪽만 줄이면 비교가 아니다."""

API_BASE_URL = "https://api.openai.com/v1"
API_KEY_ENV = "OPENAI_API_KEY"

MAX_BATCH = 64
"""요청 하나에 담는 최대 입력 수. 152 문단이면 3회 왕복이다.

큰 값이 빠르지만 실패 시 재전송량이 커지고, API 의 요청당 토큰 상한에도 걸린다."""

REQUEST_TIMEOUT_SECONDS = 60.0
"""임베딩 요청 타임아웃. 배치 64개 기준으로 넉넉하다. 무한 대기를 만들지 않는다."""


class EmbeddingConfigError(RuntimeError):
    """임베딩 클라이언트를 구성할 수 없다. 호출 전에 실패한다."""


class OpenAIEmbeddingClient:
    """OpenAI 임베딩 API 클라이언트.

    목적:
        `EmbeddingClient` 계약을 OpenAI 엔드포인트로 만족한다.

    구현 이유:
        API 키를 생성자에서 확인한다. 호출 시점에 확인하면 152 문단 색인의 첫 배치가
        나간 뒤에 실패할 수 있고, 그러면 부분 적재 상태가 남는다.

    트레이드오프:
        요청마다 클라이언트를 새로 만들지 않고 하나를 재사용한다. 커넥션 재사용으로
        빨라지지만 인스턴스를 오래 들고 있으면 소켓을 점유한다. 배치 작업이므로
        수명이 짧다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        *,
        api_key: str | None = None,
        base_url: str = API_BASE_URL,
    ) -> None:
        """API 키를 호출 전에 확인한다. 부분 적재 뒤에 실패하는 경로를 만들지 않는다."""
        key = api_key or os.environ.get(API_KEY_ENV, "")
        if not key.strip():
            msg = f"{API_KEY_ENV} 가 비어 있다. 호출 전에 실패한다"
            raise EmbeddingConfigError(msg)
        self._model = model
        self._key = key
        self._client = httpx.Client(
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {key}"},
        )

    @property
    def model_id(self) -> str:
        """DB·측정 결과에 기록되는 식별자."""
        return f"openai:{self._model}"

    @property
    def dimensions(self) -> int:
        """반환 벡터의 차원."""
        return DEFAULT_DIMENSIONS

    def _post(self, inputs: Sequence[str]) -> tuple[Vector, ...]:
        """배치 하나를 요청하고 `index` 순으로 정렬해 반환한다."""
        response = self._client.post(
            "/embeddings",
            json={"model": self._model, "input": list(inputs)},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        rows: list[dict[str, Any]] = payload["data"]
        rows.sort(key=lambda row: int(row["index"]))
        if len(rows) != len(inputs):
            msg = f"응답 개수가 다르다: 요청 {len(inputs)} 응답 {len(rows)}"
            raise ValueError(msg)

        vectors: list[Vector] = []
        for row in rows:
            vector = tuple(float(value) for value in row["embedding"])
            if len(vector) != self.dimensions:
                msg = f"차원이 {self.dimensions} 이어야 하는데 {len(vector)} 이다"
                raise ValueError(msg)
            vectors.append(vector)
        return tuple(vectors)

    def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """문단들을 벡터로 바꾼다. 입력 순서를 보존한다."""
        for text in texts:
            if not text.strip():
                msg = "빈 문자열은 임베딩할 수 없다"
                raise ValueError(msg)

        out: list[Vector] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = texts[start : start + MAX_BATCH]
            logger.info("임베딩 요청: %s, %d건", self.model_id, len(batch))
            out.extend(self._post(batch))
        return tuple(out)

    def embed_query(self, text: str) -> Vector:
        """질의 하나를 벡터로 바꾼다. 이 모델은 질의·문서가 대칭이다."""
        if not text.strip():
            msg = "빈 문자열은 임베딩할 수 없다"
            raise ValueError(msg)
        return self._post([text])[0]

    def close(self) -> None:
        """HTTP 커넥션을 닫는다."""
        self._client.close()
