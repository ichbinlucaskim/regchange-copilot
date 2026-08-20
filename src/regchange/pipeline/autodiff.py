"""새 MST 하나로 diff까지 자동으로 간다 (R-21 해소의 사용처).

목적:
    `lsJoHstInf` 폴링이 준 새 MST 하나를 받아 — 직전 MST를 찾고, 두 버전의 본문을
    확보하고, 적재하고, `change_set`을 만든다. 사람이 `from_mst`를 지정하지 않는다.

구현 이유:
    R-21 이전에는 이 경로가 없어 운영에서 diff를 돌릴 수 없었다. 수집은 되는데
    무엇이 바뀌었는지는 말하지 못하는 상태였다.

    **직전 버전 본문은 조건부로 재사용한다.** 어제 폴링에서 그것이 "새 버전"이었다면
    이미 적재돼 있다. 무조건 다시 받으면 공공 API 호출이 늘고 같은 버전의 스냅샷이
    `run_id`만 달리 중복으로 쌓인다. 그렇다고 문서 존재만 보고 재사용하면 **스냅샷 없는
    문서를 diff 근거로 쓰게 되고, 감사에서 "이 판정의 근거가 된 원문"을 제시할 수 없다.**
    그래서 세 조건을 전부 확인한다 (`decide_reuse`).

트레이드오프:
    재사용 판정이 스냅샷 파일을 한 번 읽고 해싱한다. 문서 존재 확인만 하는 것보다
    비싸지만, **그 비용이 매니페스트에 페이지별 sha256을 넣어 둔 이유**다.
    넣어 두고 확인하지 않으면 없는 것과 같다.

    `to` 버전에도 같은 판정을 적용하되 `change_set`에는 `from` 쪽만 기록한다.
    컬럼을 둘로 늘리는 대신, 짝 선택의 위험이 `from` 쪽에 있다는 사실에 맞췄다 —
    `to`는 폴링이 준 값이라 우리가 고른 것이 아니다.

엣지 케이스:
    - **제정본**: 직전 버전이 없으므로 diff를 만들지 않고 `AutoDiffSkipped`를 돌려준다.
      실패가 아니다. `resolve_previous_mst`가 `None`을 준 경우이며, 조회 실패(예외)와
      구별된다.
    - **sha256 불일치**: 재수집하되 **조용히 넘기지 않는다.** `reuse_skip_reason`에
      남고 경고 로그가 뜬다. 파일이 변조됐거나 기록이 틀렸거나 둘 중 하나이며 어느
      쪽이든 사건이다. "재수집했으니 괜찮다"가 아니다.
    - **이미 있는 change_set**: `compute_change_set`이 건너뛴다. 이 모듈은 그 판단을
      다시 하지 않는다.
    - 중간 실패: 예외가 그대로 올라간다. 앞서 커밋된 문서는 고아로 남고
      `find_orphan_documents`로 찾을 수 있다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
import structlog
from psycopg.rows import dict_row

from regchange.adapters.storage.base import DocumentStore
from regchange.ingest.client import Collection, CollectionFailure, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import MANIFEST_FILENAME, Manifest, write_snapshot
from regchange.ingest.versions import PreviousVersion, resolve_previous_mst
from regchange.store.changeset import (
    ChangeSetOutcome,
    FromDocumentSource,
    PairProvenance,
    ReuseSkipReason,
    compute_change_set,
)
from regchange.store.loader import load_snapshot

_log = structlog.get_logger(__name__)


class AutoDiffError(RuntimeError):
    """자동 diff 경로가 진행할 수 없다. **"직전 버전이 없다"와 다른 값이다**.

    후자는 `AutoDiffSkipped` 이며 정상 결과다. 이 예외는 수집 실패나 적재 결과가
    기대와 다른 경우이며, 조용히 넘기면 부분 상태가 정상으로 보인다.
    """


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """한 버전의 본문을 어디서 얻었는가.

    목적:
        "이 diff는 어떤 원문을 썼나"에 답할 수 있게 한다.

    구현 이유:
        `document_id`만 돌려주면 재사용인지 재수집인지 사라진다. 그 구별이 없으면
        나중에 스냅샷 중복이나 불일치를 추적할 단서가 없다.

    엣지 케이스:
        - `skip_reason`은 `REFETCHED`일 때만 의미가 있다. `REUSED`면 None이다.
    """

    document_id: UUID
    source: FromDocumentSource
    skip_reason: ReuseSkipReason | None


@dataclass(frozen=True, slots=True)
class AutoDiffOutcome:
    """자동 diff 결과."""

    to_mst: str
    previous: PreviousVersion
    from_document: DocumentSource
    to_document: DocumentSource
    change_set: ChangeSetOutcome


@dataclass(frozen=True, slots=True)
class AutoDiffSkipped:
    """직전 버전이 없어 diff를 만들지 않았다 — **실패가 아니다**.

    제정본이 여기 온다. 예외로 만들지 않는 이유: 제정은 정상적인 개정 유형이고
    (12개월 전수에서 법령 단위 159건), 폴링이 그것을 물어오는 것도 정상이다.
    예외로 만들면 운영 로그가 "실패"로 가득 차고 진짜 실패가 묻힌다.
    """

    to_mst: str
    reason: str = "NO_PREVIOUS_VERSION"


type AutoDiffResult = AutoDiffOutcome | AutoDiffSkipped
"""`isinstance(result, AutoDiffOutcome)`으로 좁힌다."""


async def _document_by_mst(conn: psycopg.AsyncConnection[Any], mst: str) -> dict[str, Any] | None:
    """현재 시점에 유효한 문서 행을 MST로 찾는다."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, source_key, source_run_id, source_page_sha256
              FROM regulation_document
             WHERE mst = %s AND known_until = 'infinity'
            """,
            (mst,),
        )
        return await cur.fetchone()


async def decide_reuse(
    conn: psycopg.AsyncConnection[Any], store: DocumentStore, mst: str
) -> DocumentSource | None:
    """적재된 본문을 재사용할 수 있는지 판정한다. 못 쓰면 `None`.

    목적:
        "이미 있다"의 기준을 한 곳에 못박는다.

    구현 이유:
        세 조건을 **전부** 확인한다.

        1. `regulation_document`에 해당 MST의 행이 있다
        2. 그 행의 `source_key`/`source_run_id`로 매니페스트를 찾을 수 있고,
           그 안에 `source_page_sha256`과 같은 페이지가 있다
        3. 그 페이지 파일의 **실제 sha256**이 기록된 값과 일치한다

        1번만 보면 스냅샷 없는 문서를 diff 근거로 쓰게 된다. 그러면 감사에서 원문을
        제시할 수 없고, 그것이 이 시스템이 존재하는 이유를 무너뜨린다.
        3번을 빼면 매니페스트에 sha256을 넣어 둔 것이 회수되지 않는다.

    트레이드오프:
        판정이 스냅샷 파일을 한 번 읽는다. 대형 법령이면 2.1MB다. 재수집(네트워크
        왕복 + 저장)보다 여전히 싸고, **읽지 않으면 확인한 것이 아니다.**

    엣지 케이스:
        - 문서 없음 → `None` (호출부가 `NO_DOCUMENT`로 기록)
        - 매니페스트/페이지 없음 → `None` (`NO_SNAPSHOT`)
        - **sha256 불일치 → `None`이되 호출부가 `SHA256_MISMATCH`로 구별한다.**
          "없어서 다시 받았다"와 "있는데 어긋났다"는 다른 사건이다.
        - 같은 MST의 문서가 여러 행일 수 없다 — bitemporal 상 `known_until='infinity'`
          행은 하나다.
    """
    row = await _document_by_mst(conn, mst)
    if row is None:
        return None

    manifest_key = f"{row['source_key']}/{MANIFEST_FILENAME}"
    try:
        manifest = Manifest.from_json((await store.get(manifest_key)).decode("utf-8"))
    # 저장소 구현마다 예외 타입이 다르다(Protocol 경계). 삼키지 않고 로그를 남긴 뒤
    # None 을 돌려주며, 호출부는 그것을 "재사용 불가"로 받아 재수집한다.
    except Exception:
        _log.warning("autodiff.manifest_unreadable", mst=mst, manifest_key=manifest_key)
        return None

    recorded = str(row["source_page_sha256"])
    page = next((p for p in manifest.pages if p.sha256 == recorded), None)
    if page is None:
        _log.warning("autodiff.page_not_in_manifest", mst=mst, sha256=recorded)
        return None

    body = await store.get(f"{manifest.directory}/{page.file}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != recorded:
        _log.error(
            "autodiff.snapshot_sha256_mismatch",
            mst=mst,
            file=page.file,
            recorded=recorded,
            actual=actual,
            note="저장된 파일이 변조됐거나 기록이 틀렸다. 재수집하되 원인은 미확인 "
            "— docs/incidents/ 기록 후보다",
        )
        return DocumentSource(
            document_id=row["id"],
            source=FromDocumentSource.REFETCHED,
            skip_reason=ReuseSkipReason.SHA256_MISMATCH,
        )

    return DocumentSource(document_id=row["id"], source=FromDocumentSource.REUSED, skip_reason=None)


async def _fetch_and_load(
    conn: psycopg.AsyncConnection[Any],
    client: LawApiClient,
    store: DocumentStore,
    masker: Masker,
    mst: str,
    *,
    run_id: str,
    now: dt.datetime,
) -> UUID:
    """본문을 받아 스냅샷으로 쓰고 적재한다. 적재된 문서 id를 돌려준다."""
    outcome = await client.collect(LAW_DOCUMENT, {"MST": mst})
    if isinstance(outcome, CollectionFailure):
        raise AutoDiffError(
            f"본문 수집 실패 (MST={mst}): {outcome.reason.value} / {outcome.detail}"
        )

    collection: Collection = outcome
    manifest = await write_snapshot(
        store,
        collection,
        run_id=run_id,
        fetched_at=now,
        params={"MST": mst},
        display=None,
        masker=masker,
    )
    result = await load_snapshot(conn, store, manifest, now=now)
    if len(result.documents) != 1:
        raise AutoDiffError(
            f"본문 스냅샷 하나에서 문서 {len(result.documents)}건이 나왔다 (MST={mst}). "
            "본문 계열은 페이지가 1개이므로 1건이어야 한다"
        )
    return result.documents[0].document_id


async def _ensure_document(
    conn: psycopg.AsyncConnection[Any],
    client: LawApiClient,
    store: DocumentStore,
    masker: Masker,
    mst: str,
    *,
    run_id: str,
    now: dt.datetime,
) -> DocumentSource:
    """재사용 가능하면 재사용하고 아니면 받아서 적재한다."""
    decision = await decide_reuse(conn, store, mst)
    if decision is not None and decision.source is FromDocumentSource.REUSED:
        return decision

    skip_reason = (
        decision.skip_reason
        if decision is not None and decision.skip_reason is not None
        else ReuseSkipReason.NO_DOCUMENT
        if await _document_by_mst(conn, mst) is None
        else ReuseSkipReason.NO_SNAPSHOT
    )
    document_id = await _fetch_and_load(conn, client, store, masker, mst, run_id=run_id, now=now)
    return DocumentSource(
        document_id=document_id,
        source=FromDocumentSource.REFETCHED,
        skip_reason=skip_reason,
    )


async def autodiff(
    conn: psycopg.AsyncConnection[Any],
    client: LawApiClient,
    store: DocumentStore,
    masker: Masker,
    *,
    to_mst: str,
    run_id: str,
    now: dt.datetime,
) -> AutoDiffResult:
    """새 MST 하나로 직전 버전을 찾아 diff까지 만든다.

    목적:
        운영 폴링이 부르는 진입점. 사람이 `from_mst`를 지정하지 않는다.

    구현 이유:
        `resolve_previous_mst`가 돌려준 MST를 **그대로** `PairProvenance`에 넣는다.
        그 값과 실제 비교에 쓴 문서의 MST를 `compute_change_set`이 대조해
        `mst_resolution_source`를 파생한다 — 이 함수가 "RESOLVED"라고 주장하지 않는다.
        주장하게 두면 주장과 사실이 갈릴 수 있고, 그 갈림이 바로 이 장치가 막으려는
        실패다.

    트레이드오프:
        `to` 버전에도 재사용 판정을 적용하지만 `change_set`에는 기록하지 않는다.
        컬럼을 늘리는 대신, 짝 선택의 위험이 `from` 쪽에 있다는 사실에 맞췄다.

    엣지 케이스:
        - 제정본: `AutoDiffSkipped`. 예외가 아니다.
        - `resolve_previous_mst` 실패: `VersionResolutionError`가 그대로 올라간다.
          "직전 버전이 없다"와 "확인할 수 없다"를 섞지 않는다.
        - 두 버전이 같은 문서로 해석되는 경우: `change_set`의
          `distinct_versions` CHECK가 막는다.
    """
    previous = await resolve_previous_mst(
        client, store, to_mst, run_id=run_id, fetched_at=now, masker=masker
    )
    if previous is None:
        _log.info("autodiff.skipped_no_previous", to_mst=to_mst)
        return AutoDiffSkipped(to_mst=to_mst)

    to_document = await _ensure_document(
        conn, client, store, masker, to_mst, run_id=run_id, now=now
    )
    from_document = await _ensure_document(
        conn, client, store, masker, previous.previous.mst, run_id=run_id, now=now
    )

    change_set = await compute_change_set(
        conn,
        from_document_id=from_document.document_id,
        to_document_id=to_document.document_id,
        now=now,
        provenance=PairProvenance(
            resolved_from_mst=previous.previous.mst,
            from_document_source=from_document.source,
            reuse_skip_reason=from_document.skip_reason,
        ),
    )
    return AutoDiffOutcome(
        to_mst=to_mst,
        previous=previous,
        from_document=from_document,
        to_document=to_document,
        change_set=change_set,
    )
