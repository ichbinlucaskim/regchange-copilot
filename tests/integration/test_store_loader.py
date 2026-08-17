"""스냅샷 → 파서 → 적재 경로를 끝까지 돌린다 (작업 1 + 2 + 3 결합).

이 테스트가 존재하는 이유: 세 계층이 각각 통과해도 경계에서 어긋난다. 특히
(1) 본문의 조문시행일자가 `valid_from` 으로 새어 들어가는지,
(2) 같은 스냅샷을 두 번 적재해도 중복이 생기지 않는지,
(3) 식별키가 충돌했을 때 조용히 덮어쓰지 않는지,
(4) 처분별 건수의 합이 파싱된 수와 맞는지
는 결합해 봐야만 알 수 있다. 네트워크를 쓰지 않는다 — `MockTransport` 가 픽스처를
응답으로 돌려준다.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.client import Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import Manifest, new_run_id, write_snapshot
from regchange.parse.law_xml import parse_law_document
from regchange.store.loader import (
    fetch_ministry_master,
    find_orphan_documents,
    load_document,
    load_snapshot,
)
from regchange.store.models import KeyConflictError, LoadError
from regchange.store.queries import count_pending_valid_from

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "loader-test-oc"
NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)

# 특금법 본문. 조문단위 34건, 소관부처코드 1160100(금융위원회)은 시드에 있다.
TEUKGEUM = ("law_009244_mst252787.xml", "252787", 34)


async def build_snapshot(
    tmp_path: Path,
    fixture: str,
    mst: str,
    *,
    run_seed: str = "a3f1",
) -> tuple[LocalDocumentStore, Manifest]:
    """픽스처를 수집 경로에 태워 스냅샷으로 저장한다. 실제 적재 입력과 같은 형태다."""
    body = (FIXTURES / fixture).read_bytes()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(LAW_DOCUMENT, {"MST": mst})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    store = LocalDocumentStore(tmp_path / "snapshots")
    manifest = await write_snapshot(
        store,
        outcome,
        run_id=new_run_id(NOW, entropy=run_seed),
        fetched_at=NOW,
        params={"MST": mst},
        display=None,
        masker=Masker(OC),
    )
    return store, manifest


async def test_snapshot_is_parsed_and_loaded_with_counts_that_add_up(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """적재 + 미해결적재 + 건너뜀 + 충돌 == 파싱된 수 (완료 조건 9)."""
    fixture, mst, units = TEUKGEUM
    store, manifest = await build_snapshot(tmp_path, fixture, mst)

    result = await load_snapshot(owner_conn, store, manifest, now=NOW)

    assert result.complete, "load_run 이 기록되어야 완료다"
    assert result.counts.parsed_units == units
    assert result.counts.dispositioned == units
    assert result.counts.loaded == units, "1160100 은 시드에 있으므로 전부 해결된다"
    assert result.counts.loaded_unresolved == 0
    assert result.counts.skipped == 0
    assert result.counts.key_conflicts == 0

    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM regulation_article")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == result.counts.rows_in_db == units


async def test_document_effective_date_never_becomes_valid_from(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """본문의 문서 시행일이 `valid_from` 으로 새어 들어가지 않는다.

    이것이 이 작업의 고정점이다. 본문 API 는 조문별 시행일을 문서 시행일로
    평탄화하므로(edge-case #8), 그 값을 `valid_from` 에 넣으면 원칙 6이 조용히
    무너진다 — 값이 채워지고 질의도 돌기 때문에 아무도 알아채지 못한다.
    """
    fixture, mst, _ = TEUKGEUM
    store, manifest = await build_snapshot(tmp_path, fixture, mst)
    await load_snapshot(owner_conn, store, manifest, now=NOW)

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE a.unit_type = 'ARTICLE') AS articles,
                   count(a.valid_from) AS with_valid_from,
                   count(DISTINCT a.valid_from_source) AS sources,
                   min(a.valid_from_source) AS source,
                   min(d.document_effective_date) AS doc_ef
              FROM regulation_article a
              JOIN regulation_document d ON d.id = a.document_id
            """
        )
        row = await cur.fetchone()

    assert row is not None
    assert row["total"] > 0
    assert row["with_valid_from"] == 0, "valid_from 은 한 행도 채워지지 않아야 한다"
    assert row["sources"] == 1
    assert row["source"] == "PENDING_HISTORY"
    assert row["doc_ef"] is not None, "문서 시행일은 document_effective_date 에 남는다"
    assert 0 < row["articles"] < row["total"], "제목행도 함께 적재된다 (ADR-001)"

    # 시점 질의에 잡히지 않는 조문 수가 그대로 드러난다 — 0건과 미결합을 구별한다.
    # 인용 대상은 조문 본체뿐이므로(ADR-001) 제목행은 이 숫자에 들어가지 않는다.
    assert await count_pending_valid_from(owner_conn) == row["articles"]


async def test_reloading_the_same_snapshot_skips_instead_of_duplicating(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """같은 스냅샷을 두 번 적재해도 중복이 생기지 않고, 건너뛴 수가 기록된다 (완료 조건 7).

    중복 제거로 해결하지 않는다 (edge-case #18). 식별키로 이미 있으면 건너뛴다.
    """
    fixture, mst, units = TEUKGEUM
    store, manifest = await build_snapshot(tmp_path, fixture, mst)

    first = await load_snapshot(owner_conn, store, manifest, now=NOW)
    second = await load_snapshot(owner_conn, store, manifest, now=NOW)

    assert first.counts.loaded == units
    assert second.counts.skipped == units, "두 번째는 전부 건너뛴다"
    assert second.counts.loaded == 0
    assert second.counts.rows_in_db == 0
    second.counts.verify_partition(context="재적재")

    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM regulation_article")
        articles = await cur.fetchone()
        await cur.execute("SELECT count(*) FROM regulation_document")
        documents = await cur.fetchone()
        await cur.execute("SELECT count(*) FROM load_run")
        runs = await cur.fetchone()

    assert articles is not None and articles[0] == units, "행이 늘지 않는다"
    assert documents is not None and documents[0] == 1
    assert runs is not None and runs[0] == 2, "run 기록은 두 번 남는다 — 건너뜀도 사실이다"


async def test_key_conflict_fails_loudly_and_keeps_both_rows_in_the_error(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """식별키가 같은데 내용이 다르면 조용히 덮어쓰지 않고 실패한다 (완료 조건 8).

    edge-case #18 은 진짜 중복이 35,681행에서 0건이라고 기록하되 "충돌 0건이지만
    런타임 단언으로 감시한다"로 결론지었다. 이 테스트가 그 단언을 고정한다.
    """
    fixture, mst, _ = TEUKGEUM
    store, manifest = await build_snapshot(tmp_path, fixture, mst)
    await load_snapshot(owner_conn, store, manifest, now=NOW)

    # 같은 식별키를 갖되 본문이 다른 문서를 만든다.
    tampered_xml = (
        (FIXTURES / fixture).read_text(encoding="utf-8").replace("<조문내용>", "<조문내용>변조 ", 1)
    )
    tampered = parse_law_document(tampered_xml)
    master = await fetch_ministry_master(owner_conn)

    with pytest.raises(KeyConflictError) as caught:
        await load_document(
            owner_conn,
            tampered,
            mst=mst,
            load_run_id=uuid4(),
            source_key=manifest.directory,
            source_run_id=manifest.run_id,
            page_sha256=manifest.pages[0].sha256,
            master=master,
            now=NOW,
        )

    error = caught.value
    assert error.existing, "기존 행의 전체 필드가 예외에 담긴다"
    assert error.incoming, "신규 행의 전체 필드가 예외에 담긴다"
    assert error.existing["text_norm_sha256"] != error.incoming["text_norm_sha256"]

    # 충돌한 문서의 트랜잭션은 롤백됐다 — 조문 수가 늘지 않았다.
    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM regulation_article")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == TEUKGEUM[2]


async def test_unresolved_ministry_is_loaded_but_counted_separately(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """마스터에 없는 부처는 적재하되 별도 처분으로 센다 (완료 조건 7, ADR-009).

    `loaded` 에 흡수시키면 미해결이라는 사실이 건수 안에서 사라진다. 합계도 맞고
    예외도 없으므로 아무도 모른다 — 그것이 이 프로젝트가 겪은 실패 형태다.
    """
    fixture, mst, units = TEUKGEUM
    _, manifest = await build_snapshot(tmp_path, fixture, mst)
    document = parse_law_document((FIXTURES / fixture).read_text(encoding="utf-8"))
    unknown = document.model_copy(update={"ministry_code": "9999999", "ministry": "가상부"})

    load_run_id = uuid4()
    master = await fetch_ministry_master(owner_conn)
    result = await load_document(
        owner_conn,
        unknown,
        mst=mst,
        load_run_id=load_run_id,
        source_key=manifest.directory,
        source_run_id=manifest.run_id,
        page_sha256=manifest.pages[0].sha256,
        master=master,
        now=NOW,
    )

    assert result.counts.loaded == 0
    assert result.counts.loaded_unresolved == units
    assert result.counts.rows_in_db == units, "적재는 됐다 — 부처명 하나로 조문을 막지 않는다"
    assert result.unresolved_ministry == "가상부"
    result.counts.verify_partition(context="미해결")

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT observed_name, observed_code, reason FROM ministry_unresolved")
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["observed_name"] == "가상부"
    assert rows[0]["observed_code"] == "9999999"
    assert rows[0]["reason"] == "CODE_NOT_IN_MASTER"


async def test_orphan_document_is_findable_when_run_never_completes(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """load_run 이 없는 문서는 고아로 조회된다.

    문서 단위 커밋을 택한 대가다. 남는 것을 막을 수 없다면 최소한 찾을 수 있어야
    한다 — 찾을 수 없는 부분 상태는 완전한 것과 구별되지 않는다.
    """
    fixture, mst, _ = TEUKGEUM
    _, manifest = await build_snapshot(tmp_path, fixture, mst)
    document = parse_law_document((FIXTURES / fixture).read_text(encoding="utf-8"))

    master = await fetch_ministry_master(owner_conn)
    result = await load_document(
        owner_conn,
        document,
        mst=mst,
        load_run_id=uuid4(),  # 이 run 은 끝내 load_run 을 쓰지 않는다
        source_key=manifest.directory,
        source_run_id=manifest.run_id,
        page_sha256=manifest.pages[0].sha256,
        master=master,
        now=NOW,
    )

    orphans = await find_orphan_documents(owner_conn)
    assert orphans == (result.document_id,)


async def test_manifest_without_mst_is_rejected(
    owner_conn: psycopg.AsyncConnection[Any],
    tmp_path: Path,
) -> None:
    """MST 가 없는 매니페스트는 대체값을 지어내지 않고 실패한다.

    본문 응답에는 MST 가 없다 — 요청 파라미터가 유일한 출처다.
    """
    fixture, mst, _ = TEUKGEUM
    store, manifest = await build_snapshot(tmp_path, fixture, mst)
    stripped = dataclasses.replace(manifest, params={})

    with pytest.raises(LoadError, match="MST"):
        await load_snapshot(owner_conn, store, stripped, now=NOW)
