"""그래프 실행과 재개 — 커넥션·모델·체크포인터를 한 곳에서 조립한다.

목적:
    개정 조문 하나에 대해 그래프를 끝까지(승인 대기까지) 돌리고, 검토자의 판단으로
    중단된 그래프를 재개한다.

구현 이유:
    **조립을 한 곳에 모은다.** 커넥션 두 개(`app_graph` / `app_review`), 모델 클라이언트,
    임베딩, 저장소, 체크포인터가 함께 있어야 그래프가 돈다. 이 조립이 흩어지면 "어느
    role 로 붙었는가"가 호출부마다 달라지고, 그때 원칙 5 의 경계가 조립 실수 하나에 걸린다.

    **재개가 새 실행과 같은 조립을 쓴다.** 승인 이후 노드만 도는 재개에도 `app_graph`
    커넥션이 필요하다 — 체크포인터가 그 커넥션과 별개이긴 하지만, 노드 서명이 같은
    `GraphDeps` 를 받기 때문이다. 재개 경로만 다른 조립을 쓰면 그 경로가 다른 권한으로
    돌 수 있게 된다.

    **`interrupt` 가 걸렸는지 반환값으로 확인한다.** 실행 결과에 `__interrupt__` 가 있으면
    승인 대기이고, 없으면 그래프가 끝난 것이다(근거 부족으로 이관). 이 구별을 호출부가
    할 수 있어야 "승인 대기 중"과 "이관됨"이 같은 성공으로 보이지 않는다.

트레이드오프:
    - 실행마다 그래프를 새로 컴파일한다. 의존성이 실행마다 다르므로 전역 캐시를 두면
      테스트가 실제 커넥션을 공유하게 된다. 컴파일 비용은 무시할 수 있다.
    - 커넥션을 컨텍스트 매니저로 열고 닫는다. 장시간 프로세스라면 풀이 필요하지만,
      지금은 조문 한 건 처리와 검토 재개가 각각 짧은 작업이다.

엣지 케이스:
    - **체크포인터를 열 수 없음**: 예외를 전파한다. 승인 대기가 성립하지 않는 상태에서
      그래프를 도는 경로를 만들지 않는다.
    - **재개했는데 그 스레드가 중단 상태가 아님**: 그래프가 아무 노드도 실행하지 않고
      최종 상태를 돌려준다. 승인 레코드가 생기지 않으므로 API 가 409 로 잡는다.
    - **이미 결정된 평가를 다시 재개**: `record_decision` 이 `ReviewError` 를 던지고,
      그 예외가 여기서 전파된다. 두 번째 승인이 발송 대상을 하나 더 만들지 않는다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from langgraph.types import Command

from regchange.adapters.embedding import EmbeddingClient
from regchange.adapters.llm import LLMClient
from regchange.adapters.storage import DocumentStore
from regchange.adapters.switches import PostgresSwitchStore
from regchange.graph.build import build_graph, checkpointer
from regchange.graph.nodes import GraphDeps
from regchange.guards.killswitch import SwitchGate
from regchange.pipeline.impact import GroundingMode
from regchange.retrieval.models import SearchMode
from regchange.store.dsn import DbRole, role_dsn

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphRunner:
    """조립된 그래프 하나. 실행과 재개가 같은 의존성을 공유한다."""

    graph: Any
    deps: GraphDeps

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """개정 조문 하나로 그래프를 시작하고 승인 대기(또는 종료)까지 간다.

        반환값에 `__interrupt__` 가 있으면 승인 대기다. 없으면 근거 부족으로 이관된
        것이며, **둘을 같은 성공으로 읽지 않는다.**
        """
        thread = payload.get("thread_id") or payload["assessment_id"]
        config = {"configurable": {"thread_id": str(thread)}}
        result = await self.graph.ainvoke(payload, config=config)
        interrupted = "__interrupt__" in result
        logger.info(
            "그래프 실행 종료: %s (%s)",
            result.get("status"),
            "승인 대기" if interrupted else "종료 — 사람에게 가지 않았다",
        )
        return dict(result)

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        """중단된 그래프를 검토자의 판단으로 재개한다.

        `Command(resume=...)` 외의 재개 경로를 만들지 않는다. 다른 경로가 있으면
        그것이 곧 승인 우회다 (원칙 4).
        """
        config = {"configurable": {"thread_id": thread_id}}
        result = await self.graph.ainvoke(Command(resume=decision), config=config)
        logger.info(
            "그래프 재개: thread=%s decision=%s outbox=%s",
            thread_id,
            decision.get("decision"),
            result.get("outbox_ids"),
        )
        return dict(result)


@asynccontextmanager
async def open_runner(
    *,
    llm: LLMClient,
    embedding: EmbeddingClient,
    store: DocumentStore,
    as_of: dt.date | None = None,
    top_k: int = 10,
    mode: SearchMode = SearchMode.HYBRID,
    promote: bool = True,
    grounding: GroundingMode = GroundingMode.ANCHORED,
    switches: SwitchGate | None = None,
) -> AsyncIterator[GraphRunner]:
    """커넥션 두 개와 체크포인터를 열어 그래프를 조립한다.

    목적:
        실행에 필요한 자원을 한 번에 열고 닫는다.

    구현 이유:
        **킬 스위치 게이트를 `app_graph` role 로 만든다.** 그래프가 자기 role 로 스위치를
        읽는다 — 별도 role 로 읽으면 "스위치는 읽히는데 정작 그래프는 못 도는" 조합이
        가능해지고, 그 조합은 조용하다. `switches` 를 주입할 수 있게 둔 이유는 측정과
        테스트가 값을 고정해야 하기 때문이며, 기본값은 **실제 DB 를 읽는 게이트**다.

        `app_graph` 와 `app_review` 를 **따로 연다.** 하나로 합치면 role 도 하나가 되고,
        그러면 LLM 노드가 승인 레코드를 쓸 수 있는 상태가 된다 — 경계가 사라진다.

    트레이드오프:
        커넥션 세 개(그래프·검토·체크포인터)를 동시에 연다. 체크포인터가 별도인 이유는
        `search_path` 가 달라야 하기 때문이다 (`graph/build.py`).

    엣지 케이스:
        모듈 docstring 참조.
    """
    async with (
        await psycopg.AsyncConnection.connect(role_dsn(DbRole.GRAPH)) as graph_conn,
        await psycopg.AsyncConnection.connect(role_dsn(DbRole.REVIEW)) as review_conn,
        checkpointer() as saver,
    ):
        deps = GraphDeps(
            graph_conn=graph_conn,
            review_conn=review_conn,
            switches=switches or SwitchGate(store=PostgresSwitchStore(role_dsn(DbRole.GRAPH))),
            llm=llm,
            embedding=embedding,
            store=store,
            as_of=as_of or dt.datetime.now(dt.UTC).date(),
            top_k=top_k,
            mode=mode,
            promote=promote,
            grounding=grounding,
        )
        yield GraphRunner(graph=build_graph(deps, checkpointer=saver), deps=deps)
