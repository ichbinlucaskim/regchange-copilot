"""`regchange` 명령. 운영·재현 진입점.

목적:
    직전 MST 조회, 자동 diff, 수동 지정 diff 세 가지를 명령으로 노출한다.

구현 이유:
    **수동 지정 경로를 없애지 않는다.** 자동 경로가 생겼다고 수동을 지우면 골든셋
    재현과 회귀 테스트가 불가능해진다. 두 경로가 같은 `compute_change_set`을 통과하고,
    어느 쪽으로 왔는지는 `mst_resolution_source`로 구별된다 — 경로를 지우는 대신
    **어느 경로였는지 기록하는 쪽**을 택했다.

    `law previous`를 별도 명령으로 둔 이유: R-21의 해소 경로가 실제로 무엇을 돌려주는지
    사람이 확인할 수 있어야 한다. 자동 diff 안에 묻어 두면 "왜 이 버전과 비교했나"를
    물었을 때 파이프라인 전체를 돌려야 답이 나온다.

트레이드오프:
    `argparse` 서브커맨드를 쓴다. `typer`/`click`을 쓰면 짧아지지만 의존성이 늘고,
    이 CLI는 명령 3개짜리라 표준 라이브러리로 충분하다.

    DSN을 `owner_dsn()`으로 잡는다. 적재와 diff 계산이 쓰기를 하므로 읽기 전용 role은
    맞지 않다. **원칙 5의 읽기 전용 경계는 LLM 프로세스에 대한 것**이며 이 CLI는
    거기 해당하지 않는다 — 이 명령은 프롬프트도 모델 출력도 보지 않는다.

엣지 케이스:
    - 제정본에 `diff auto`를 걸면 실패가 아니라 "직전 버전 없음"으로 끝난다.
      종료 코드 0이다. 제정은 정상적인 개정 유형이다.
    - `.env`가 없으면 `owner_dsn()`/환경변수 조회가 실패한다. 잡지 않는다 —
      설정 누락을 조용한 기본값으로 덮지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

import httpx
import psycopg

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.client import LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.snapshot import new_run_id, utc_now
from regchange.ingest.versions import resolve_previous_mst
from regchange.pipeline import AutoDiffOutcome, autodiff
from regchange.store.changeset import compute_change_set
from regchange.store.dsn import owner_dsn

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = REPO_ROOT / "data" / "snapshots"


def _emit(line: str) -> None:
    """표준출력에 한 줄 쓴다.

    `print` 를 쓰지 않는 이유는 스타일이 아니라 규칙이다 — `src/` 에 lint 예외를
    만들지 않기로 했고(ADR-012, `client.py` 의 `_default_jitter` 와 같은 판단),
    `T201` 을 `noqa` 로 끄면 그 예외가 하나 생긴다. 함수 하나로 끝나는 일에
    저장소 전체의 규칙을 흔들지 않는다.
    """
    sys.stdout.write(f"{line}\n")


def _load_env() -> dict[str, str]:
    """`.env`를 읽어 키-값으로 돌려준다. 없으면 빈 매핑."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.split("#")[0].strip()
    return env


def _base_url(env: dict[str, str]) -> str:
    """`.env`의 전체 URL에서 endpoint 앞까지를 잘라낸다."""
    configured = env.get("LAW_GO_KR_BASE_URL", "").strip()
    if configured.endswith(".do"):
        return configured.rsplit("/", 1)[0]
    return configured.rstrip("/")


async def _cmd_law_previous(mst: str) -> int:
    """직전 MST를 조회해 출력한다. 적재도 diff도 하지 않는다."""
    env = _load_env()
    oc = env["LAW_GO_KR_OC"]
    store = LocalDocumentStore(SNAPSHOT_ROOT)
    now = utc_now()
    async with httpx.AsyncClient() as http:
        client = LawApiClient(_base_url(env), http, oc)
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
    env = _load_env()
    oc = env["LAW_GO_KR_OC"]
    store = LocalDocumentStore(SNAPSHOT_ROOT)
    now = utc_now()
    run_id = new_run_id(now)

    async with (
        httpx.AsyncClient() as http,
        await psycopg.AsyncConnection.connect(owner_dsn()) as conn,
    ):
        client = LawApiClient(_base_url(env), http, oc)
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

    return parser


def main() -> None:
    """인자를 파싱하고 해당 명령을 실행한다."""
    args = build_parser().parse_args()

    if args.group == "law":
        code = asyncio.run(_cmd_law_previous(args.mst))
    elif args.command == "auto":
        code = asyncio.run(_cmd_diff_auto(args.mst))
    else:
        code = asyncio.run(_cmd_diff_manual(args.from_document_id, args.to_document_id))

    raise SystemExit(code)


if __name__ == "__main__":
    main()
