"""`regchange` 명령. 운영·재현 진입점.

목적:
    직전 MST 조회, 자동/수동 diff, 그리고 **일일 운영(`ops`)** 을 명령으로 노출한다.
    cron 이 부르는 것도 이 CLI 다 — 셸 스크립트에 로직을 두지 않는다.

구현 이유:
    **수동 지정 경로를 없애지 않는다.** 자동 경로가 생겼다고 수동을 지우면 골든셋
    재현과 회귀 테스트가 불가능해진다. 두 경로가 같은 `compute_change_set`을 통과하고,
    어느 쪽으로 왔는지는 `mst_resolution_source`로 구별된다 — 경로를 지우는 대신
    **어느 경로였는지 기록하는 쪽**을 택했다.

    `law previous`를 별도 명령으로 둔 이유: R-21의 해소 경로가 실제로 무엇을 돌려주는지
    사람이 확인할 수 있어야 한다. 자동 diff 안에 묻어 두면 "왜 이 버전과 비교했나"를
    물었을 때 파이프라인 전체를 돌려야 답이 나온다.

    **`.env` 로딩을 여기서 한다.** cron/launchd 는 사용자 셸 환경을 상속하지 않아
    `DATABASE_URL` 도 `LAW_GO_KR_OC` 도 없다. 래퍼 셸에서 export 하면 수동 실행과
    cron 실행의 환경이 갈리므로, 두 경로가 같은 코드를 지나게 한다.

트레이드오프:
    `argparse` 서브커맨드를 쓴다. `typer`/`click`을 쓰면 짧아지지만 의존성이 늘고,
    이 CLI는 표준 라이브러리로 충분하다.

    DSN을 `owner_dsn()`으로 잡는다. 적재와 diff 계산이 쓰기를 하므로 읽기 전용 role은
    맞지 않다. **원칙 5의 읽기 전용 경계는 LLM 프로세스에 대한 것**이며 이 CLI는
    거기 해당하지 않는다 — 이 명령은 프롬프트도 모델 출력도 보지 않는다.

엣지 케이스:
    - 제정본에 `diff auto`를 걸면 실패가 아니라 "직전 버전 없음"으로 끝난다.
      종료 코드 0이다. 제정은 정상적인 개정 유형이다.
    - `.env`가 없어도 환경변수가 주어졌으면 동작한다. 둘 다 없으면 `SettingsError`
      로 그 자리에서 실패한다 — 설정 누락을 조용한 기본값으로 덮지 않는다.
    - `ops daily` 가 중간에 예외로 죽으면 **실패 행을 남기고** 종료 코드 1 을 준다.
      기록 없이 죽으면 그날은 "실행하지 않은 날"로 보이며, 그것은 다른 사실이다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any
from uuid import UUID

import httpx
import psycopg

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.config.corpus import load_corpus_config
from regchange.config.settings import apply_dotenv, law_api_base_url, law_api_oc, snapshot_root
from regchange.guards.killswitch import Switch
from regchange.ingest.client import LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.snapshot import new_run_id, utc_now
from regchange.ingest.versions import resolve_previous_mst
from regchange.ops import (
    DEFAULT_LOOKBACK_DAYS,
    fetch_alerts,
    fetch_history,
    fetch_summary,
    lookback_dates,
    record_failure,
    record_run,
    run_daily,
)
from regchange.ops.history import DEFAULT_ALERT_DAYS, DEFAULT_HISTORY_DAYS
from regchange.ops.models import OpsRunStatus
from regchange.ops.render import render_alerts, render_daily, render_history, render_summary
from regchange.ops.switches import current_switches, set_switch, switch_history
from regchange.pipeline import AutoDiffOutcome, autodiff
from regchange.store.changeset import compute_change_set
from regchange.store.dsn import owner_dsn


def _emit(line: str) -> None:
    """표준출력에 한 줄 쓴다.

    `print` 를 쓰지 않는 이유는 스타일이 아니라 규칙이다 — `src/` 에 lint 예외를
    만들지 않기로 했고(ADR-012, `client.py` 의 `_default_jitter` 와 같은 판단),
    `T201` 을 `noqa` 로 끄면 그 예외가 하나 생긴다. 함수 하나로 끝나는 일에
    저장소 전체의 규칙을 흔들지 않는다.
    """
    sys.stdout.write(f"{line}\n")


def _emit_all(lines: list[str]) -> None:
    """줄 목록을 순서대로 출력한다."""
    for line in lines:
        _emit(line)


async def _cmd_law_previous(mst: str) -> int:
    """직전 MST를 조회해 출력한다. 적재도 diff도 하지 않는다."""
    oc = law_api_oc()
    store = LocalDocumentStore(snapshot_root())
    now = utc_now()
    async with httpx.AsyncClient() as http:
        client = LawApiClient(law_api_base_url(), http, oc)
        previous = await resolve_previous_mst(
            client, store, mst, run_id=new_run_id(now), fetched_at=now, masker=Masker(oc)
        )

    if previous is None:
        _emit(f"MST {mst}: 직전 버전 없음 (제정본). 신구법존재여부=N")
        return 0

    header = previous.previous
    _emit(f"MST {mst} 의 직전 버전: {header.mst}")
    _emit(f"  법령ID     {header.law_id}   {header.law_name}")
    _emit(
        f"  공포        {header.promulgation_date} "
        f"제{header.promulgation_no}호 {header.revision_kind}"
    )
    _emit(f"  시행        {header.effective_date}")
    return 0


async def _cmd_diff_auto(mst: str) -> int:
    """직전 MST를 스스로 찾아 diff까지 만든다."""
    oc = law_api_oc()
    store = LocalDocumentStore(snapshot_root())
    now = utc_now()
    run_id = new_run_id(now)

    async with (
        httpx.AsyncClient() as http,
        await psycopg.AsyncConnection.connect(owner_dsn()) as conn,
    ):
        client = LawApiClient(law_api_base_url(), http, oc)
        result = await autodiff(conn, client, store, Masker(oc), to_mst=mst, run_id=run_id, now=now)

    if not isinstance(result, AutoDiffOutcome):
        _emit(f"MST {mst}: 직전 버전이 없어 diff 를 만들지 않았다 ({result.reason})")
        return 0

    change_set = result.change_set
    _emit(f"MST {result.previous.previous.mst} → {mst}")
    skip = result.from_document.skip_reason
    detail = "" if skip is None else f" ({skip.value})"
    _emit(f"  from 문서   {result.from_document.source.value}{detail}")
    _emit(f"  change_set  {change_set.change_set_id} (created={change_set.created})")
    if change_set.result is not None:
        counts = change_set.result.counts
        _emit(
            f"  건수        +{counts.added} -{counts.deleted} ~{counts.modified} "
            f"편집{counts.editorial} 동일{counts.unchanged}"
        )
    return 0


async def _cmd_diff_manual(from_document_id: UUID, to_document_id: UUID) -> int:
    """이미 적재된 문서 두 개를 지정해 diff 한다. 골든셋 재현 경로다."""
    async with await psycopg.AsyncConnection.connect(owner_dsn()) as conn:
        outcome = await compute_change_set(
            conn,
            from_document_id=from_document_id,
            to_document_id=to_document_id,
            now=utc_now(),
        )
    _emit(f"change_set {outcome.change_set_id} (created={outcome.created}, source=MANUAL)")
    return 0


async def _cmd_ops_daily(days: int, dates: tuple[str, ...] | None) -> int:
    """일일 작업을 돈다. cron 이 부르는 명령이다.

    목적:
        카나리아 → 최근 N일 폴링 → 코퍼스 필터 → 적재 → diff → 실행 이력 기록.

    구현 이유:
        **예외를 잡아 실패 행을 남긴다.** 기반 실패(저장소·DB)로 죽으면 그날은
        기록이 없고, 기록이 없는 날은 "노트북이 꺼져 있던 날"과 같은 모양이 된다.
        두 상태는 조치가 다르므로 구별해서 남긴다.

        종료 코드를 상태에서 파생시킨다. cron 은 0/비-0 만 보므로, 실패·부분 실패를
        비-0 으로 돌려주면 래퍼 스크립트나 감시 도구가 그것을 볼 수 있다.

    트레이드오프:
        `SKIPPED_CANARY_FAILED` 도 비-0 으로 돌려준다. 미수행은 우리 실패가 아니지만
        **사람이 봐야 하는 상태**이며, 종료 코드는 실패율 지표가 아니라 신호다.
        실패율은 `ops summary` 가 상태 값으로 계산하므로 여기서 구별할 필요가 없다.

    엣지 케이스:
        - `--date` 를 주면 재확인 창 대신 그 날짜만 돈다. 특정 날짜를 다시 돌리는
          운영·재현 경로이며, 멱등성 때문에 이미 처리한 것은 다시 처리되지 않는다.
        - DB 에 접속조차 못 하면 실패 행도 남길 수 없다. 그 경우 예외가 그대로
          올라가고 래퍼 스크립트의 로그가 유일한 흔적이다.
    """
    oc = law_api_oc()
    store = LocalDocumentStore(snapshot_root())
    corpus = load_corpus_config()
    now = utc_now()
    run_id = new_run_id(now)
    targets = dates if dates else lookback_dates(now, days)

    async with (
        httpx.AsyncClient() as http,
        await psycopg.AsyncConnection.connect(owner_dsn()) as conn,
    ):
        client = LawApiClient(law_api_base_url(), http, oc)
        try:
            result = await run_daily(
                conn,
                client,
                store,
                Masker(oc),
                corpus=corpus,
                dates=targets,
                run_id=run_id,
                now=now,
            )
        # 법령 단위 실패는 `run_daily` 가 이미 값으로 바꿨다. 여기까지 오는 것은
        # 기반 실패이며, 삼키지 않고 **실패 행으로 기록한 뒤** 비-0 으로 끝낸다.
        except Exception as exc:
            await conn.rollback()
            await record_failure(
                conn,
                run_id=run_id,
                started_at=now,
                dates=targets,
                detail=f"{type(exc).__name__}: {exc}",
            )
            _emit(f"실행 실패: {type(exc).__name__}: {exc}")
            raise

        await record_run(conn, result)

    _emit_all(render_daily(result))
    return 0 if result.status in {OpsRunStatus.SUCCEEDED, OpsRunStatus.SUCCEEDED_ZERO} else 1


async def _with_conn(query: Any) -> int:
    """읽기 전용 조회 명령의 공통 껍데기. 커넥션을 열고 줄을 출력한다."""
    async with await psycopg.AsyncConnection.connect(owner_dsn()) as conn:
        lines = await query(conn)
    _emit_all(lines)
    return 0


async def _cmd_ops_history(days: int) -> int:
    """실행 이력을 최신순으로 출력한다."""

    async def query(conn: psycopg.AsyncConnection[Any]) -> list[str]:
        rows = await fetch_history(conn, days=days, now=utc_now())
        return render_history(rows, days=days)

    return await _with_conn(query)


async def _cmd_ops_summary() -> int:
    """운영 시작일부터 오늘까지를 집계해 출력한다."""

    async def query(conn: psycopg.AsyncConnection[Any]) -> list[str]:
        return render_summary(await fetch_summary(conn, now=utc_now()))

    return await _with_conn(query)


async def _cmd_ops_alerts(days: int) -> int:
    """최근 N일의 알림을 출력한다. 기록만 되고 보이지 않던 값을 모은다."""

    async def query(conn: psycopg.AsyncConnection[Any]) -> list[str]:
        alerts = await fetch_alerts(conn, days=days, now=utc_now())
        return render_alerts(alerts, days=days)

    return await _with_conn(query)


def _cmd_review_serve(host: str, port: int) -> int:
    """검토 UI 를 띄운다. 승인은 **그래프 재개로만** 이루어진다 (원칙 4).

    목적:
        담당자가 검토 대기 목록을 보고 수락·수정·반려를 보내는 화면을 연다.

    구현 이유:
        `resume` 을 여기서 조립해 앱에 주입한다. API 모듈이 그래프를 만들면 검토 화면이
        모델·임베딩·커넥션 구성에 결합되고, 테스트가 화면을 띄우려면 실제 모델이 필요해진다.

        요청마다 러너를 새로 연다. 커넥션·체크포인터를 앱 수명 동안 붙들고 있으면 유휴
        커넥션이 오래 열려 있게 되고, 검토는 며칠에 한 번 일어나는 일이다.

    트레이드오프:
        재개마다 임베딩 클라이언트를 만든다. 로컬 임베딩은 로딩이 무겁지만, 승인 이후
        노드는 검색을 하지 않으므로 실제로 쓰이지 않는다 — 그럼에도 만드는 이유는
        `GraphDeps` 가 완전한 조립을 요구하기 때문이며, 부분 조립을 허용하면 어느 노드가
        무엇을 쓰는지가 조립부의 지식이 된다.

    엣지 케이스:
        - 승인 대기가 아닌 스레드를 재개: 그래프가 아무 노드도 실행하지 않고 API 가 409 를
          돌려준다. 조용히 성공으로 보이지 않는다.
    """
    import uvicorn

    from regchange.adapters.embedding.local import LocalEmbeddingClient
    from regchange.adapters.llm.claude import ClaudeClient
    from regchange.api.app import create_app
    from regchange.graph.runner import open_runner

    async def resume(thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        async with open_runner(
            llm=ClaudeClient(),
            embedding=LocalEmbeddingClient(),
            store=LocalDocumentStore(snapshot_root()),
        ) as runner:
            return await runner.resume(thread_id, decision)

    uvicorn.run(create_app(resume=resume), host=host, port=port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """명령 트리를 만든다. 테스트가 인자 파싱만 검증할 수 있게 분리했다."""
    parser = argparse.ArgumentParser(prog="regchange", description="법령 버전 조회와 조문 차분")
    sub = parser.add_subparsers(dest="group", required=True)

    law = sub.add_parser("law", help="법령 버전 조회")
    law_sub = law.add_subparsers(dest="command", required=True)
    previous = law_sub.add_parser("previous", help="직전 버전의 MST 를 조회한다")
    previous.add_argument("--mst", required=True, help="새 버전의 법령일련번호")

    diff = sub.add_parser("diff", help="두 버전 비교")
    diff_sub = diff.add_subparsers(dest="command", required=True)
    auto = diff_sub.add_parser("auto", help="직전 MST 를 스스로 찾아 diff 한다")
    auto.add_argument("--mst", required=True, help="새 버전의 법령일련번호")
    manual = diff_sub.add_parser("manual", help="적재된 문서 두 개를 지정해 diff 한다")
    manual.add_argument("--from-document-id", required=True, type=UUID)
    manual.add_argument("--to-document-id", required=True, type=UUID)

    ops = sub.add_parser("ops", help="일일 운영과 실행 이력")
    ops_sub = ops.add_subparsers(dest="command", required=True)

    daily = ops_sub.add_parser("daily", help="일일 작업 (cron 진입점)")
    daily.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"최근 며칠을 재확인하는가 (기본 {DEFAULT_LOOKBACK_DAYS}일)",
    )
    daily.add_argument(
        "--date",
        action="append",
        dest="dates",
        metavar="YYYYMMDD",
        help="이 날짜만 처리한다 (여러 번 지정 가능). 주면 --days 를 무시한다",
    )

    history = ops_sub.add_parser("history", help="실행 이력")
    history.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)

    ops_sub.add_parser("summary", help="운영 시작일부터 오늘까지의 집계")

    alerts = ops_sub.add_parser("alerts", help="MISMATCH·변경규모·연속 0건·카나리아 실패·검토 기한")
    alerts.add_argument("--days", type=int, default=DEFAULT_ALERT_DAYS)

    review = sub.add_parser("review", help="검토 큐")
    review_sub = review.add_subparsers(dest="command", required=True)
    serve = review_sub.add_parser("serve", help="검토 UI 를 띄운다")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    switch = sub.add_parser("switch", help="킬 스위치 — 재배포 없이 기능을 멈춘다")
    switch_sub = switch.add_subparsers(dest="command", required=True)
    switch_sub.add_parser("list", help="현재 값과 마지막 변경 사유")
    switch_history_cmd = switch_sub.add_parser("history", help="변경 이력 (감사용)")
    switch_history_cmd.add_argument("--name", choices=[s.value for s in Switch], default=None)
    switch_history_cmd.add_argument("--limit", type=int, default=20)
    for verb, enabled in (("on", True), ("off", False)):
        action = switch_sub.add_parser(verb, help=f"스위치를 {'켠다' if enabled else '끈다'}")
        action.add_argument("name", choices=[s.value for s in Switch])
        # --by 와 --reason 은 필수다. 기본값을 두면 기본값이 기록된다.
        action.add_argument("--by", required=True, help="바꾸는 사람")
        action.add_argument("--reason", required=True, help="왜 바꾸는가 (필수)")

    return parser


def _dispatch_switch(args: argparse.Namespace) -> int:
    """`switch` 하위 명령을 고른다."""
    if args.command == "list":
        return asyncio.run(_cmd_switch_list())
    if args.command == "history":
        return asyncio.run(_cmd_switch_history(args.name, args.limit))
    return asyncio.run(_cmd_switch_set(args.name, args.command == "on", args.by, args.reason))


def _dispatch_ops(args: argparse.Namespace) -> int:
    """`ops` 하위 명령을 고른다."""
    if args.command == "daily":
        dates = tuple(args.dates) if args.dates else None
        return asyncio.run(_cmd_ops_daily(args.days, dates))
    if args.command == "history":
        return asyncio.run(_cmd_ops_history(args.days))
    if args.command == "summary":
        return asyncio.run(_cmd_ops_summary())
    return asyncio.run(_cmd_ops_alerts(args.days))


async def _cmd_switch_list() -> int:
    """스위치 현재 값과 마지막 변경 사유를 출력한다."""

    async def query(conn: psycopg.AsyncConnection[Any]) -> list[str]:
        current = await current_switches(conn)
        lines = ["킬 스위치 현재 상태", ""]
        for switch in Switch:
            state = current.get(switch.value)
            if state is None:
                lines.append(f"  {switch.value:<18} 꺼짐 (설정된 적 없음 — 기본값)")
                continue
            mark = "켜짐" if state.enabled else "꺼짐"
            lines.append(
                f"  {switch.value:<18} {mark}  ({state.changed_by}, "
                f"{state.changed_at:%Y-%m-%d %H:%M %Z}) — {state.reason}"
            )
        lines.extend(["", "반영은 최대 60초 걸린다. 즉시 반영이 필요하면 프로세스를 재시작한다."])
        return lines

    return await _with_conn(query)


async def _cmd_switch_history(name: str | None, limit: int) -> int:
    """스위치 변경 이력을 최신순으로 출력한다. 감사 질문의 답이 여기 있다."""

    async def query(conn: psycopg.AsyncConnection[Any]) -> list[str]:
        rows = await switch_history(conn, switch=Switch(name) if name else None, limit=limit)
        if not rows:
            return ["변경 이력이 없다. 스위치가 설정된 적이 없으면 전부 꺼짐이다."]
        lines = ["킬 스위치 변경 이력 (최신순)", ""]
        for row in rows:
            mark = "켜짐" if row.enabled else "꺼짐"
            until = "" if row.known_until.year > 9000 else f" → {row.known_until:%Y-%m-%d %H:%M}"
            lines.append(
                f"  {row.known_from:%Y-%m-%d %H:%M}{until}  {row.name:<18} {mark}  "
                f"{row.changed_by}: {row.reason}"
            )
        return lines

    return await _with_conn(query)


async def _cmd_switch_set(name: str, enabled: bool, by: str, reason: str) -> int:
    """스위치를 켜거나 끈다. **소유자 커넥션으로 쓴다** — 서비스 role 에는 권한이 없다."""
    async with await psycopg.AsyncConnection.connect(owner_dsn()) as conn:
        await set_switch(conn, switch=Switch(name), enabled=enabled, changed_by=by, reason=reason)
    _emit_all(
        [
            f"{name} = {'켜짐' if enabled else '꺼짐'} ({by}: {reason})",
            "반영까지 최대 60초. 즉시 필요하면 프로세스를 재시작한다.",
        ]
    )
    return 0


def main() -> None:
    """인자를 파싱하고 해당 명령을 실행한다. `.env` 로딩이 첫 단계다."""
    args = build_parser().parse_args()
    apply_dotenv()

    if args.group == "ops":
        code = _dispatch_ops(args)
    elif args.group == "switch":
        code = _dispatch_switch(args)
    elif args.group == "review":
        code = _cmd_review_serve(args.host, args.port)
    elif args.group == "law":
        code = asyncio.run(_cmd_law_previous(args.mst))
    elif args.command == "auto":
        code = asyncio.run(_cmd_diff_auto(args.mst))
    else:
        code = asyncio.run(_cmd_diff_manual(args.from_document_id, args.to_document_id))

    raise SystemExit(code)


if __name__ == "__main__":
    main()
