"""임베딩 어댑터 경계 — 도메인 코드는 이 Protocol 만 본다 (ADR-010).

목적:
    텍스트→벡터 변환을 공급자와 무관한 하나의 계약으로 노출한다.

구현 이유:
    구현체(`local`, `openai`)를 여기서 재수출하지 않는다. `adapters/storage` 가
    `DocumentStore` Protocol 만 재수출하고 `LocalDocumentStore` 는 하위 모듈에
    둔 것과 같다. 재수출하면 도메인 코드가 `from regchange.adapters.embedding import
    LocalEmbeddingClient` 를 쓰게 되고, import-linter 계약이 잡기 전까지 그것이
    가장 편한 길이 된다.

    조립은 설정 코드(측정 스크립트, 4단계의 그래프 구성)가 한다. 어느 임베딩을 쓸지는
    **배포 형태의 결정**이며 도메인 로직이 알 일이 아니다 — 특히 로컬/외부 API 선택은
    클라우드 심의 결과에 따라 바뀔 수 있다 (축 2).

트레이드오프:
    호출부가 구현 모듈 경로를 알아야 해서 import 가 길어진다. 그 마찰이 경계를
    지키게 한다.

엣지 케이스:
    - 구현체가 하나도 설치되지 않은 환경: 이 패키지를 import 하는 것만으로는 실패하지
      않는다. `local` 구현은 `sentence-transformers` 를 함수 안에서 import 하므로,
      임베딩을 실제로 쓰지 않는 경로(수집·파싱·차분)는 torch 없이 돌아간다.
"""

from regchange.adapters.embedding.base import EmbeddingClient, Vector

__all__ = ["EmbeddingClient", "Vector"]
