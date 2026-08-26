"""로컬 임베딩 구현 — 외부로 텍스트를 보내지 않는다.

목적:
    `sentence-transformers` 로 로컬에서 임베딩을 만든다. 기본 모델은 BGE-m3
    (`BAAI/bge-m3`, 1024차원) 이며 한국어를 포함한 다국어로 학습됐다.

구현 이유:
    **사내 규정 원문이 프로세스 밖으로 나가지 않는다.** 이것이 축 2(은행 IT 제약)에서
    실제로 걸리는 지점이다. 이 저장소의 코퍼스는 합성이지만, 같은 코드가 실제 규정을
    다루게 되는 순간 외부 API 호출은 클라우드 이용 심의 대상이 된다. 심의를 통과할 수
    있느냐와 별개로, **통과하지 않아도 되는 경로가 존재한다는 것**이 설계상 가치다.

    **`sentence_transformers` 를 함수 안에서 import 한다.** torch 는 무겁고, 이
    저장소의 수집·파싱·차분 경로는 임베딩을 쓰지 않는다. 모듈 최상단에서 import 하면
    `regchange.adapters` 를 건드리는 모든 코드가 torch 로딩 비용을 낸다. 의존성도
    `eval` 그룹에만 둔다 — 운영 배포에서 이 구현을 쓰지 않기로 결정하면 torch 를
    설치하지 않아도 된다.

    **정규화된 벡터를 반환한다.** pgvector 의 코사인 거리(`<=>`)는 정규화 여부와
    무관하게 옳지만, 정규화해 두면 내적과 코사인이 같아져 나중에 연산자를 바꿔도
    순위가 흔들리지 않는다.

트레이드오프:
    첫 실행에서 모델 가중치(약 2GB)를 내려받는다. 오프라인 환경에서는 미리 받아
    캐시를 옮겨야 한다. 그 비용을 지불하는 대신 호출당 비용과 외부 의존이 0이다.

    추론 속도가 API 보다 느리다. 152 문단 색인은 CPU 에서도 수십 초 안이고, 질의는
    건당 수백 밀리초다. 3단계 측정에서는 문제가 되지 않지만, 대화형 검색 UI(4단계)에
    붙일 때는 다시 재야 한다.

    `sentence-transformers` 는 타입 스텁을 제공하지 않는다. mypy strict 하에서
    `Any` 가 경계에 나타나며, 이는 CLAUDE.md §4 가 허용하는 "외부 경계"에 해당한다.
    좁힐 수 없는 이유는 라이브러리가 반환 타입을 런타임 인자(`convert_to_numpy`)에
    따라 바꾸기 때문이다.

엣지 케이스:
    - 빈 문자열: `ValueError`. 영벡터를 돌려주면 코사인 거리가 정의되지 않는다.
    - 모델을 내려받을 수 없음: `sentence-transformers` 의 예외를 그대로 전파한다.
      "임베딩 없이 계속"하는 경로를 만들지 않는다 — 벡터 없는 검색은 다른 검색이다.
    - 최대 토큰(8192) 초과: 모델이 조용히 자른다. 조 단위 문단은 최대 486자이므로
      현재 코퍼스에서는 발생하지 않으며, 발생하면 절단 사실이 지표에 드러나지 않는다.
      그래서 입력 길이를 검사해 경고 로그를 남긴다.
    - 모델 로딩은 인스턴스당 한 번이다. 매 호출 로딩하면 152 문단 색인이 시간 단위가 된다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from regchange.adapters.embedding.base import Vector

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "BAAI/bge-m3"
"""기본 로컬 모델. 다국어 학습이며 한국어 법령·규정 문체에서 널리 쓰인다.

`dimensions` 를 상수로 박지 않고 모델에게 물어본다 — 모델을 바꿨는데 상수를 안 고치면
DB 의 CHECK 제약이 잡기 전까지 조용히 어긋난다."""

MAX_CHARS_WARN = 4000
"""이 길이를 넘는 입력은 절단 가능성을 경고한다.

BGE-m3 의 최대 입력은 8192 토큰이고 한국어는 대략 1자당 1토큰 안팎이다. 4000자는
그 절반으로 둔 여유값이며, 넘었다고 실패시키지는 않는다 — 절단은 모델의 동작이고
우리가 할 일은 그것이 조용히 일어나지 않게 하는 것이다."""


class LocalEmbeddingClient:
    """로컬 `sentence-transformers` 모델로 임베딩을 만든다.

    목적:
        외부 전송 없이 `EmbeddingClient` 계약을 만족한다.

    구현 이유:
        모델을 게으르게 로딩한다. 생성자에서 로딩하면 측정 스크립트가 인자 파싱
        단계에서 수십 초를 쓰고, 잘못된 인자로 실행했을 때도 그 비용을 낸다.

    트레이드오프:
        첫 호출이 느리다. 그 대신 인스턴스를 만드는 것 자체는 무료라 설정 코드에서
        자유롭게 조립할 수 있다.

    엣지 케이스:
        모듈 docstring 참조.
    """

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL) -> None:
        """모델 이름만 받아 둔다. 실제 로딩은 첫 호출 때 일어난다."""
        self._model_name = model_name
        self._model: Any | None = None

    @property
    def model_id(self) -> str:
        """DB·측정 결과에 기록되는 식별자. 공급자 접두어로 API 모델과 구분한다."""
        return f"local:{self._model_name}"

    @property
    def dimensions(self) -> int:
        """모델이 보고하는 출력 차원. 상수로 박지 않는다.

        메서드 이름이 라이브러리 버전에 따라 다르다(`get_embedding_dimension` 이
        신형, `get_sentence_embedding_dimension` 이 구형이며 후자는 FutureWarning 을
        낸다). 둘 다 받아 두는 이유는, 차원을 상수로 박는 대안이 더 나쁘기 때문이다 —
        모델을 바꿨는데 상수를 안 고치면 DB CHECK 가 잡기 전까지 조용히 어긋난다.
        """
        model = self._loaded()
        getter = getattr(model, "get_embedding_dimension", None)
        if getter is None:
            getter = model.get_sentence_embedding_dimension
        dimension: int = getter()
        return dimension

    def _loaded(self) -> Any:
        """모델을 한 번만 로딩한다. `Any` 인 이유는 라이브러리에 스텁이 없기 때문이다."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("로컬 임베딩 모델 로딩: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _encode(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """공통 인코딩 경로. 질의와 문서가 같은 전처리를 쓰도록 한 곳에 둔다."""
        for text in texts:
            if not text.strip():
                msg = "빈 문자열은 임베딩할 수 없다"
                raise ValueError(msg)
            if len(text) > MAX_CHARS_WARN:
                logger.warning("입력이 길어 절단될 수 있다: %d자", len(text))

        encoded: Any = self._loaded().encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors = tuple(tuple(float(value) for value in row) for row in encoded)
        expected = self.dimensions
        for vector in vectors:
            if len(vector) != expected:
                msg = f"차원이 {expected} 이어야 하는데 {len(vector)} 이다"
                raise ValueError(msg)
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """문단들을 벡터로 바꾼다. 입력 순서를 보존한다."""
        if not texts:
            return ()
        return self._encode(texts)

    def embed_query(self, text: str) -> Vector:
        """질의 하나를 벡터로 바꾼다.

        BGE-m3 는 질의·문서 접두어를 요구하지 않는 대칭 모델이므로 문서와 같은 경로를
        쓴다. 비대칭 모델로 바꿀 때 이 메서드만 고치면 된다 — 경계가 나뉘어 있는 이유다.
        """
        return self._encode([text])[0]
