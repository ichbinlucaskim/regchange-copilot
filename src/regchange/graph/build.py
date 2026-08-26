"""그래프 조립과 체크포인터 — 승인 게이트를 구조로 만든다 (원칙 4, ADR-013).

목적:
    노드를 간선으로 잇고 Postgres 체크포인터를 붙여, **승인 없이 발송 대상에 도달하는
    경로가 존재하지 않는** 그래프를 만든다.

구현 이유:
    **`enqueue_actions` 로 들어오는 간선이 `human_review` 하나뿐이다.** 이것이 원칙 4 의
    실체다. UI 검사는 API 를 직접 호출하면 우회되지만, 그래프에 다른 경로가 없으면 우회할
    대상 자체가 없다. 이 성질은 주석이 아니라 **테스트로 고정한다**
    (`tests/security/test_graph_approval_gate.py`) — 프레임워크 업그레이드로 `interrupt`
    동작이 바뀌면 그 테스트가 먼저 깨져야 한다 (ADR-013 엣지 케이스).

    **체크포인터를 별도 스키마에 둔다** (기획서 13.4, 마이그레이션 011). `search_path` 로
    `graph_checkpoint` 를 지정한 커넥션을 쓰며, 그 스키마는 `app_graph` 소유다. 체크포인트에는
    프롬프트와 모델 출력이 들어 있으므로 **발송 워커는 USAGE 조차 없다** (원칙 5).

    **`setup()` 을 마이그레이션이 아니라 런타임에 부른다.** 체크포인터의 테이블 정의는
    라이브러리 버전에 따라 바뀌고, 그것을 우리 마이그레이션에 복사하면 버전을 올릴 때마다
    두 곳을 맞춰야 한다. 대신 스키마와 권한은 우리가 만들고, 그 안의 테이블은 라이브러리가
    만든다 — **경계는 우리 것이고 내용은 라이브러리 것이다.**

    **상태 스키마를 우리가 통제한다** (`graph/state.py`). 직렬화기가 dataclass 의 `tuple`
    을 `list` 로 되돌리는 것을 실측했고, "등록되지 않은 타입은 앞으로 차단된다"는 경고도
    확인했다. 그래서 상태에는 평범한 사전만 넣는다. ADR-013 이 프레임워크에 대해 경계한
    지점 — *"프레임워크 동작에 대한 잘못된 가정"* — 이 실제로 나타난 자리다.

트레이드오프:
    - 노드를 `functools.partial` 로 감싸 의존성을 주입한다. LangGraph 의 `config` 를
      쓰는 방식이 관용적이지만 커넥션은 직렬화할 수 없고, **체크포인트에 들어가면 안 되는
      것은 상태에도 config 에도 두지 않는다**는 규칙을 지켰다.
    - 그래프를 매 실행마다 새로 만든다. 컴파일 비용이 있지만 의존성이 실행마다 다르고,
      전역으로 두면 테스트가 실제 커넥션을 공유하게 된다.

엣지 케이스:
    - **체크포인터 없이 컴파일**: 허용한다. 다만 `human_review` 에서 `interrupt` 가 실패한다.
      승인 없는 실행 경로를 만들지 않기 위해 **"체크포인터가 없으면 승인을 건너뛴다"는
      분기를 두지 않았다** — 그런 분기가 곧 우회 경로다.
    - **재개 시 입력이 바뀐 경우**: 체크포인트의 상태를 쓰므로 새 입력은 무시된다.
      `Command(resume=...)` 는 판단만 전달한다.
    - **같은 `thread_id` 로 두 번 실행**: 체크포인터가 이어서 실행한다. 새 평가를 원하면
      새 `thread_id` 를 써야 하며, 기본값은 평가 id 이므로 매번 다르다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any
from urllib.parse import quote

from langgraph.graph import END, START, StateGraph

from regchange.graph.nodes import (
    GraphDeps,
    bump_revision,
    draft_impact,
    enqueue_actions,
    extract_obligations_node,
    grounding_gate,
    human_review,
    load_change,
    persist_assessment,
    record_rejection,
    retrieve_policy,
    route_after_persist,
    route_after_review,
    sanitize_input,
    verify_citations,
)
from regchange.graph.state import AssessmentState
from regchange.store.dsn import DbRole, role_dsn

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA = "graph_checkpoint"
"""체크포인트 테이블이 사는 스키마. 마이그레이션 011 이 만들고 `app_graph` 가 소유한다."""


def checkpoint_dsn() -> str:
    """체크포인터용 DSN — `app_graph` 로 붙되 `search_path` 를 체크포인트 스키마로 둔다.

    목적:
        라이브러리가 자기 테이블을 `public` 이 아니라 격리된 스키마에 만들게 한다.

    구현 이유:
        체크포인터 구현이 스키마 인자를 받지 않는다. `search_path` 는 라이브러리를 고치지
        않고 대상 스키마를 정하는 유일한 수단이며, 커넥션 옵션이므로 그 커넥션에만 적용된다.

    트레이드오프:
        DSN 문자열을 조작한다. 이미 `options` 가 붙은 DSN 이 주어지면 덮어쓰지 않고
        예외를 던진다 — 조용히 합치면 어느 `search_path` 가 이겼는지 알 수 없다.

    엣지 케이스:
        - 환경변수로 준 DSN 에 `options` 가 이미 있음: `ValueError`.
    """
    dsn = role_dsn(DbRole.GRAPH)
    if "options=" in dsn:
        msg = (
            f"체크포인터 DSN 에 이미 options 가 있다: {dsn!r}. "
            f"search_path 를 {CHECKPOINT_SCHEMA} 로 강제할 수 없어 중단한다"
        )
        raise ValueError(msg)
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={quote(f'-c search_path={CHECKPOINT_SCHEMA}')}"


@asynccontextmanager
async def checkpointer() -> AsyncIterator[Any]:
    """Postgres 체크포인터를 열고 테이블을 준비한다.

    `setup()` 은 멱등이며 매번 부른다. 부르지 않으면 첫 실행이 "테이블이 없다"로 실패하고,
    그 실패는 배포 시점이 아니라 첫 승인 대기에서 나타난다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn()) as saver:
        await saver.setup()
        yield saver


def build_graph(deps: GraphDeps, *, checkpointer: Any | None = None) -> Any:
    """노드를 잇고 컴파일한다. `enqueue_actions` 의 유일한 진입점은 `human_review` 다.

    목적:
        4단계 지시 §5 의 그래프를 그대로 만든다. 다만 검색과 추출의 **순서는 데이터 의존을
        따랐다** (`graph/nodes.py` 구현 이유 참조).

    구현 이유:
        조건 분기를 셋 둔다.

          | 분기 | 어디서 | 무엇을 가르는가 |
          |---|---|---|
          | `grounding_gate` | 검증 뒤 | 재작성할 것인가, 적재로 갈 것인가 |
          | `route_after_persist` | 적재 뒤 | 사람에게 갈 것인가, 여기서 끝낼 것인가 |
          | `route_after_review` | 승인 뒤 | 발송 대상을 만들 것인가, 반려만 기록할 것인가 |

        재작성 루프가 `bump_revision → draft_impact` 로 돌아간다. 회차 증가를 별도 노드로
        둔 이유는 분기 함수를 순수하게 유지하기 위해서다 — 분기에서 상태를 바꾸면 재개할
        때마다 다시 바뀐다.

    트레이드오프:
        노드 수가 늘어 그림이 복잡해진다. 그 대신 각 경계에서 체크포인트가 남고, 재개
        지점이 실제 비용 경계(모델 호출 전후)와 일치한다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    graph: StateGraph[AssessmentState, None, AssessmentState, AssessmentState] = StateGraph(
        AssessmentState
    )

    def node(fn: Callable[..., Any]) -> Callable[..., Any]:
        """의존성을 주입한 노드로 감싼다. 상태에는 커넥션이 들어가지 않는다."""
        return partial(fn, deps=deps)

    graph.add_node("load_change", node(load_change))
    graph.add_node("sanitize_input", node(sanitize_input))
    graph.add_node("retrieve_policy", node(retrieve_policy))
    graph.add_node("extract_obligations", node(extract_obligations_node))
    graph.add_node("draft_impact", node(draft_impact))
    graph.add_node("verify_citations", node(verify_citations))
    graph.add_node("bump_revision", node(bump_revision))
    graph.add_node("persist_assessment", node(persist_assessment))
    graph.add_node("human_review", human_review)
    graph.add_node("enqueue_actions", node(enqueue_actions))
    graph.add_node("record_rejection", node(record_rejection))

    graph.add_edge(START, "load_change")
    graph.add_edge("load_change", "sanitize_input")
    graph.add_edge("sanitize_input", "retrieve_policy")
    graph.add_edge("retrieve_policy", "extract_obligations")
    graph.add_edge("extract_obligations", "draft_impact")
    graph.add_edge("draft_impact", "verify_citations")

    # evaluator-optimizer — 재작성은 1회로 제한된다 (ADR-013, MAX_REVISIONS).
    graph.add_conditional_edges(
        "verify_citations",
        grounding_gate,
        {"rewrite": "bump_revision", "persist_assessment": "persist_assessment"},
    )
    graph.add_edge("bump_revision", "draft_impact")

    # 근거가 부족하면 사람에게 가지 않고 끝난다. "영향 없음"이 아니라 "모른다"다.
    graph.add_conditional_edges(
        "persist_assessment",
        route_after_persist,
        {"human_review": "human_review", END: END},
    )

    # 승인 게이트. 이 아래의 두 노드는 human_review 를 거치지 않고 도달할 수 없다.
    graph.add_conditional_edges(
        "human_review",
        route_after_review,
        {"enqueue_actions": "enqueue_actions", "record_rejection": "record_rejection"},
    )
    graph.add_edge("enqueue_actions", END)
    graph.add_edge("record_rejection", END)

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info(
        "그래프 컴파일: 노드 11개, 체크포인터 %s",
        "있음" if checkpointer is not None else "없음 — 승인 대기가 성립하지 않는다",
    )
    return compiled
