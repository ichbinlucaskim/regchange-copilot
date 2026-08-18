"""change_set 적재 — 건수 단언, 멱등성, 그리고 DB 가 강제하는 것들.

이 테스트가 존재하는 이유: 순수 판정이 맞아도 적재 경계에서 어긋날 수 있다. 특히
(1) 조문 개수 보존이 DB CHECK 로도 강제되는가,
(2) 같은 버전 쌍을 두 번 계산해도 change_set 이 중복되지 않는가,
(3) 이동 후보의 status 가 PENDING 외의 값을 가질 수 없는가 (ADR-003),
(4) EDITORIAL 이 행으로 남고 우선순위만 내려가는가
는 DB 에 물어봐야 알 수 있다.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.client import Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import new_run_id, write_snapshot
from regchange.store.changeset import compute_change_set
from regchange.store.loader import load_snapshot

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "changeset-test-oc"
NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.UTC)

OLD = ("law_009244_mst113262_v20110519.xml", "113262", "a001")
NEW = ("law_009244_mst215971_v20200324.xml", "215971", "b002")


async def _load(
    conn: psycopg.AsyncConnection[Any], tmp_path: Path, fixture: str, mst: str, seed: str
) -> UUID:
    """픽스처를 수집 경로에 태워 적재하고 문서 id 를 돌려준다."""
    body = (FIXTURES / fixture).read_bytes()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(LAW_DOCUMENT, {"MST": mst})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    store = LocalDocumentStore(tmp_path / f"snap-{mst}")
    manifest = await write_snapshot(
        store,
        outcome,
        run_id=new_run_id(NOW, entropy=seed),
        fetched_at=NOW,
        params={"MST": mst},
        display=None,
        masker=Masker(OC),
    )
    result = await load_snapshot(conn, store, manifest, now=NOW)
    return result.documents[0].document_id


@pytest.fixture
async def versions(owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path) -> tuple[UUID, UUID]:
    """특금법 2011판과 2020판을 적재해 두 문서 id 를 준다."""
    old = await _load(owner_conn, tmp_path, *OLD)
    new = await _load(owner_conn, tmp_path, *NEW)
    return old, new


async def test_change_set_is_written_with_counts_that_check_out(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """건수가 DB CHECK 를 통과한다 — 조문 개수 보존이 스키마로도 강제된다."""
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    assert outcome.created
    assert outcome.result is not None

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM change_set WHERE id = %s", (outcome.change_set_id,))
        row = await cur.fetchone()

    assert row is not None
    assert row["from_article_count"] == 19
    assert row["to_article_count"] == 27
    assert row["deleted"] + row["modified"] + row["editorial"] + row["unchanged"] == 19
    assert row["added"] + row["modified"] + row["editorial"] + row["unchanged"] == 27


async def test_revision_kind_is_recorded(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """제개정구분이 change_set 에 남는다 (완료 조건 3).

    타법개정은 조문 이벤트의 56.2% 를 차지하는데 변경사유로는 식별되지 않는다.
    우선순위 정책은 분석 계층이 정하며 여기서는 기록만 한다.
    """
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "SELECT revision_kind FROM change_set WHERE id = %s", (outcome.change_set_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "일부개정", "2020-03-24 공포판의 제개정구분"


async def test_move_window_counts_are_recorded(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """창 안·밖 표기 건수가 남는다 — 필터가 조용히 버리는 경로가 되지 않는다."""
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT moves_in_window, moves_out_of_window, out_of_window_dates, "
            "candidate_pool_size FROM change_set WHERE id = %s",
            (outcome.change_set_id,),
        )
        row = await cur.fetchone()

    assert row is not None
    assert row["moves_in_window"] == 27
    assert row["moves_out_of_window"] == 0
    assert row["out_of_window_dates"] == []
    assert row["candidate_pool_size"] > 0


async def test_editorial_rows_are_kept_and_demoted(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """EDITORIAL 은 행으로 남고 우선순위만 9 가 된다. DB CHECK 가 그 대응을 강제한다."""
    old, new = versions
    await compute_change_set(owner_conn, from_document_id=old, to_document_id=new, now=NOW)

    async with owner_conn.cursor() as cur:
        # EDITORIAL 인데 강등되지 않은 행은 CHECK 위반이므로 존재할 수 없다.
        await cur.execute(
            "SELECT count(*) FROM article_change "
            "WHERE (change_type = 'EDITORIAL') <> (priority_rank = 9)"
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_move_candidates_are_always_pending(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """명시 표기가 있어도 status 는 PENDING 이고, 다른 값은 DB 가 거부한다 (ADR-003).

    이 테스트가 막는 것: "법제처가 명시했으니 자동 확정하자"는 코드가 들어오는 것.
    애플리케이션 조건문이 아니라 CHECK 제약이 막는다.
    """
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    async with owner_conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM article_move_candidate "
            "WHERE change_set_id = %s AND evidence_kind = 'EXPLICIT'",
            (outcome.change_set_id,),
        )
        explicit = await cur.fetchone()
        await cur.execute("SELECT count(*) FROM article_move_candidate WHERE status <> 'PENDING'")
        confirmed = await cur.fetchone()

    assert explicit is not None and explicit[0] == 12
    assert confirmed is not None and confirmed[0] == 0

    with pytest.raises(psycopg.errors.CheckViolation):
        async with owner_conn.cursor() as cur:
            await cur.execute(
                "UPDATE article_move_candidate SET status = 'CONFIRMED' WHERE change_set_id = %s",
                (outcome.change_set_id,),
            )
    await owner_conn.rollback()


async def test_evidence_keeps_the_parsed_raw_text(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """EXPLICIT 후보에 파싱 원문이 남는다 — 텍스트에서 추출한 것이므로 사후 검증이 가능해야 한다."""
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT evidence FROM article_move_candidate "
            "WHERE change_set_id = %s AND evidence_kind = 'EXPLICIT' LIMIT 1",
            (outcome.change_set_id,),
        )
        row = await cur.fetchone()

    assert row is not None
    assert "이동" in str(row["evidence"]["explicit_raw"])
    assert row["evidence"]["similarity"] is not None, "세 신호가 함께 남는다"


async def test_recomputing_the_same_pair_is_skipped_not_duplicated(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """같은 버전 쌍을 다시 계산하면 건너뛴다 (완료 조건 10).

    중복 제거로 해결하지 않는다 (edge-case #18). 기존 행을 다시 계산해 비교하지도
    않는다 — 결과가 달라졌을 때 무엇이 맞는지 판단할 근거가 없기 때문이다.
    """
    old, new = versions
    first = await compute_change_set(owner_conn, from_document_id=old, to_document_id=new, now=NOW)
    second = await compute_change_set(owner_conn, from_document_id=old, to_document_id=new, now=NOW)

    assert first.created is True
    assert second.created is False
    assert second.change_set_id == first.change_set_id
    assert second.result is None

    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM change_set")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_comparing_different_laws_is_rejected(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """다른 법령끼리 비교하면 실패한다.

    전 조문이 ADDED/DELETED 로 잡히고 그 결과는 그럴듯해 보이므로, 예외로 막는다.
    """
    from regchange.diff import DiffError

    old, new = versions
    async with owner_conn.cursor() as cur:
        await cur.execute(
            "UPDATE regulation_document SET known_until = %s WHERE id = %s",
            (dt.datetime(2026, 9, 1, tzinfo=dt.UTC), old),
        )
    await owner_conn.rollback()

    with pytest.raises(DiffError, match="문서를 찾을 수 없다"):
        await compute_change_set(owner_conn, from_document_id=uuid4(), to_document_id=new, now=NOW)


async def test_reversed_promulgation_order_is_rejected(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """공포일자가 역전되면 실패한다 — 날짜 창이 뒤집혀 이동이 전부 창 밖이 된다."""
    from regchange.diff import DiffError

    old, new = versions
    with pytest.raises(DiffError, match="역전"):
        await compute_change_set(owner_conn, from_document_id=new, to_document_id=old, now=NOW)
