"""수집 → 적재 → 차분을 잇는 오케스트레이션 경계.

목적:
    `ingest`(외부 API), `store`(적재·차분), `diff`(순수 계산)를 한 흐름으로 엮는다.
    각 패키지는 서로를 모르고, 그 조립을 이 패키지만 안다.

구현 이유:
    조립을 `store` 안에 두면 적재 코드가 API 클라이언트를 알게 되고, `ingest` 안에
    두면 수집 코드가 DB를 알게 된다. 둘 다 CLAUDE.md §8의 경계를 흐린다.
    **조립은 그 자체로 하나의 관심사**이므로 별도 패키지에 둔다.

    `diff`·`temporal`·`verification` 이 I/O를 import 하지 않는다는 계약(원칙 1)은
    그대로다. 이 패키지는 그 반대편 — I/O를 아는 쪽 — 이며, 순수 계층을 오염시키지
    않는다.

트레이드오프:
    호출 그래프에 한 겹이 늘어난다. 그 대신 "어디서 API를 부르고 어디서 커밋하는가"가
    한 파일 안에 모여, 실패 시 무엇이 커밋됐고 무엇이 안 됐는지 읽을 수 있다.

엣지 케이스:
    - 이 패키지는 상태를 갖지 않는다. 연결·클라이언트·저장소를 전부 주입받는다.
      전역을 두면 테스트가 실제 API를 때리는 경로가 생긴다.
"""

from regchange.pipeline.autodiff import (
    AutoDiffOutcome,
    AutoDiffSkipped,
    DocumentSource,
    autodiff,
    decide_reuse,
)

__all__ = [
    "AutoDiffOutcome",
    "AutoDiffSkipped",
    "DocumentSource",
    "autodiff",
    "decide_reuse",
]
