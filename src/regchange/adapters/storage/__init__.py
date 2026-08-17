"""저장소 어댑터 패키지. 인터페이스만 재수출한다 (ADR-010).

목적:
    도메인 코드가 `from regchange.adapters.storage import DocumentStore` 로
    **인터페이스만** 가져가게 한다. 구현체는 이 이름공간에 올리지 않는다.

구현 이유:
    구현체를 여기서 재수출하면 `ingest` 가 실수로 구체 클래스를 가져올 수 있고,
    import-linter 계약이 `storage.local` 을 금지해도 `storage` 를 경유해
    우회된다. **경계를 강제하려면 우회 경로를 만들지 않아야 한다** — ADR-010이
    "인터페이스만 있고 강제가 없으면 6주 뒤 무너진다"고 적은 것과 같은 이유다.

    구현체는 조립 지점(진입점·테스트)에서 `regchange.adapters.storage.local` 을
    직접 import 한다. 그 경로가 길고 명시적인 것이 의도다 — 도메인 코드에서
    그것을 쓰면 눈에 띈다.

트레이드오프:
    구현체를 쓰는 쪽의 import 문이 길어진다. 편의를 포기한 대신, 어떤 코드가
    구체 구현에 의존하는지가 import 문만 봐도 드러나고 CI 가 그것을 검사한다.

엣지 케이스:
    - 여기에 구현체를 추가하고 싶어지면 그것은 조립 지점이 잘못 설계됐다는
      신호다. 재수출을 늘리지 말고 의존성 주입 위치를 옮긴다.
    - `DocumentStore` 와 DB Protocol 두 종류가 한 패키지에 있다. 둘 다 "저장"이나
      대상이 다르다(관계형 행 vs 원문 스냅샷). 분리가 필요해지면 패키지를 나누되
      **인터페이스와 구현의 분리가 우선이다.**
"""

from regchange.adapters.storage.base import (
    ApprovedWriteStore,
    DocumentStore,
    ReadOnlyStore,
    Row,
)

__all__ = [
    "ApprovedWriteStore",
    "DocumentStore",
    "ReadOnlyStore",
    "Row",
]
