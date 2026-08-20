"""R-21 해소가 만든 새 실패에 대한 탐지 장치 — **실제로 발화하는지** 본다.

이 테스트가 존재하는 이유: 자동 확보 경로가 생기면서 "조용히 잘못된 비교"가 가능해졌다.
직전 MST 를 잘못 고르면 diff 가 틀린 결과를 내는데 예외도 경고도 나지 않고 결과는
그럴듯해 보인다. 방어 장치를 만들어 두고 발화시켜 보지 않으면 그것이 작동하는지 모른다.

여기서 발화시키는 것 네 가지:
  §3-1 법령ID 불일치      → DiffError
  §3-2 공포일자 역전      → DiffError
  §3-3 연속성(MISMATCH)   → change_set.mst_resolution_source
  §3-4 변경 규모 이상      → change_set.change_ratio_exceeded

그리고 재사용 판정 세 분기(REUSED / NO_DOCUMENT / SHA256_MISMATCH).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.diff import DiffCounts, DiffError
from regchange.ingest.client import Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import new_run_id, write_snapshot
from regchange.pipeline import DocumentSource, decide_reuse
from regchange.store.changeset import (
    CHANGE_RATIO_WARN,
    FromDocumentSource,
    MstResolutionSource,
    PairProvenance,
    ReuseSkipReason,
    change_ratio,
    compute_change_set,
    resolve_mst_source,
)
from regchange.store.loader import load_snapshot

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "autodiff-test-oc"
NOW = dt.datetime(2026, 8, 19, 9, 0, tzinfo=dt.UTC)

OLD = ("law_009244_mst113262_v20110519.xml", "113262", "c001")
NEW = ("law_009244_mst215971_v20200324.xml", "215971", "c002")
OTHER_LAW = ("law_010199_mst280277.xml", "280277", "c003")


async def _load(
    conn: psycopg.AsyncConnection[Any],
    root: Path,
    fixture: str,
    mst: str,
    seed: str,
) -> tuple[UUID, LocalDocumentStore]:
    """픽스처를 수집 경로에 태워 적재하고 (문서 id, 저장소) 를 돌려준다."""
    body = (FIXTURES / fixture).read_bytes()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(LAW_DOCUMENT, {"MST": mst})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    store = LocalDocumentStore(root)
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
    return result.documents[0].document_id, store


@pytest.fixture
async def versions(owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path) -> tuple[UUID, UUID]:
    """특금법 2011판(113262)과 2020판(215971)을 적재한다."""
    old, _ = await _load(owner_conn, tmp_path / "old", *OLD)
    new, _ = await _load(owner_conn, tmp_path / "new", *NEW)
    return old, new


async def _row(conn: psycopg.AsyncConnection[Any], change_set_id: UUID) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM change_set WHERE id = %s", (change_set_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# §3-3 연속성 — 이번 작업의 핵심
# ---------------------------------------------------------------------------


def test_resolution_source_is_derived_not_asserted() -> None:
    """세 값이 자동 확보값과 실제 사용값의 대조에서 파생된다."""
    assert (
        resolve_mst_source(resolved_from_mst=None, actual_from_mst="113262")
        is MstResolutionSource.MANUAL
    )
    assert (
        resolve_mst_source(resolved_from_mst="113262", actual_from_mst="113262")
        is MstResolutionSource.RESOLVED
    )
    assert (
        resolve_mst_source(resolved_from_mst="999999", actual_from_mst="113262")
        is MstResolutionSource.MISMATCH
    )


async def test_mismatch_is_recorded_when_resolved_and_used_mst_differ(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """**MISMATCH 발화.** 자동 확보값과 실제 비교에 쓴 MST 가 다르면 DB 에 남는다.

    실패시키지 않는 이유: 수동 지정 경로를 유지하기로 했으므로 불일치가 정상적으로
    발생한다(골든셋 재현). 대신 조용히 넘기지 않는다 — 이 값이 없으면 나중에
    "왜 이 diff 가 이상하지"를 추적할 방법이 없다.
    """
    old, new = versions
    outcome = await compute_change_set(
        owner_conn,
        from_document_id=old,
        to_document_id=new,
        now=NOW,
        provenance=PairProvenance(resolved_from_mst="999999"),
    )

    row = await _row(owner_conn, outcome.change_set_id)
    assert row["mst_resolution_source"] == MstResolutionSource.MISMATCH.value
    assert row["resolved_from_mst"] == "999999"


async def test_resolved_is_recorded_when_they_agree(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """자동 확보값이 실제 사용값과 같으면 RESOLVED 로 남는다."""
    old, new = versions
    outcome = await compute_change_set(
        owner_conn,
        from_document_id=old,
        to_document_id=new,
        now=NOW,
        provenance=PairProvenance(
            resolved_from_mst="113262",
            from_document_source=FromDocumentSource.REUSED,
        ),
    )

    row = await _row(owner_conn, outcome.change_set_id)
    assert row["mst_resolution_source"] == MstResolutionSource.RESOLVED.value
    assert row["from_document_source"] == FromDocumentSource.REUSED.value


async def test_manual_path_still_works_and_is_labelled(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """수동 지정 경로가 살아 있고 MANUAL 로 남는다 (골든셋 재현 경로).

    자동 경로가 생겼다고 수동을 지우면 골든셋 재현과 회귀 테스트가 불가능해진다.
    """
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    row = await _row(owner_conn, outcome.change_set_id)
    assert row["mst_resolution_source"] == MstResolutionSource.MANUAL.value
    assert row["resolved_from_mst"] is None


# ---------------------------------------------------------------------------
# §3-4 변경 규모 이상
# ---------------------------------------------------------------------------


def test_change_ratio_is_bounded_and_uses_the_larger_side() -> None:
    """분모가 큰 쪽이며 조문 0건이면 나눗셈을 하지 않는다."""
    assert change_ratio(DiffCounts()) == 0.0
    assert change_ratio(
        DiffCounts(from_article_count=100, to_article_count=100, modified=5, unchanged=95)
    ) == pytest.approx(0.05)
    # 다른 법령을 비교하면 전 조문이 ADDED/DELETED 로 잡혀 1.0 에 가깝다.
    assert change_ratio(
        DiffCounts(from_article_count=19, to_article_count=80, added=80, deleted=19)
    ) == pytest.approx((80 + 19) / 80)


async def test_change_ratio_exceeded_fires_on_a_nine_year_gap(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """**규모 이상 발화.** 특금법 2011 → 2020 은 전 조문이 바뀌어 비율이 1.0 이다.

    실제 사례다 — 9년 간격의 두 버전을 비교하면 이렇게 된다. 다른 법령을 비교했을
    때도 같은 모양이 나오므로, 이 경고는 "짝이 이상하다"의 신호다.
    """
    old, new = versions
    outcome = await compute_change_set(
        owner_conn, from_document_id=old, to_document_id=new, now=NOW
    )

    assert outcome.result is not None
    assert change_ratio(outcome.result.counts) > CHANGE_RATIO_WARN

    row = await _row(owner_conn, outcome.change_set_id)
    assert row["change_ratio_exceeded"] is True


# ---------------------------------------------------------------------------
# §3-1 / §3-2 — 이미 있던 장치도 발화시켜 본다
# ---------------------------------------------------------------------------


async def test_different_law_ids_are_rejected(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path, versions: tuple[UUID, UUID]
) -> None:
    """**법령ID 불일치 발화.** 다른 법령끼리는 어떤 경우에도 비교하지 않는다."""
    _, new = versions
    other, _store = await _load(owner_conn, tmp_path / "other", *OTHER_LAW)

    with pytest.raises(DiffError, match="다른 법령끼리"):
        await compute_change_set(owner_conn, from_document_id=other, to_document_id=new, now=NOW)


async def test_reversed_promulgation_dates_are_rejected(
    owner_conn: psycopg.AsyncConnection[Any], versions: tuple[UUID, UUID]
) -> None:
    """**공포일자 역전 발화.** 창이 뒤집히면 이동 표기가 전부 창 밖이 된다."""
    old, new = versions

    with pytest.raises(DiffError, match="공포일자가 역전"):
        await compute_change_set(owner_conn, from_document_id=new, to_document_id=old, now=NOW)


# ---------------------------------------------------------------------------
# 재사용 판정 세 분기
# ---------------------------------------------------------------------------


async def test_reuse_requires_document_and_snapshot_and_matching_hash(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path
) -> None:
    """세 조건이 다 맞으면 REUSED 다."""
    root = tmp_path / "reuse"
    _document_id, store = await _load(owner_conn, root, *OLD)

    decision = await decide_reuse(owner_conn, store, "113262")

    assert isinstance(decision, DocumentSource)
    assert decision.source is FromDocumentSource.REUSED
    assert decision.skip_reason is None


async def test_unknown_mst_is_not_reusable(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path
) -> None:
    """문서가 없으면 재사용 대상이 아니다."""
    store = LocalDocumentStore(tmp_path / "empty")

    assert await decide_reuse(owner_conn, store, "999999") is None


async def test_tampered_snapshot_is_detected_and_not_reused(
    owner_conn: psycopg.AsyncConnection[Any], tmp_path: Path
) -> None:
    """**sha256 불일치 발화.** 저장된 파일이 바뀌면 재사용하지 않고 사유를 남긴다.

    "재수집했으니 괜찮다"가 아니다. 파일이 변조됐거나 기록이 틀렸거나 둘 중 하나이며
    어느 쪽이든 사건이다. 그래서 `NO_SNAPSHOT` 이 아니라 별도 값으로 구별한다.
    """
    root = tmp_path / "tamper"
    _document_id, store = await _load(owner_conn, root, *NEW)

    page = next(p for p in root.rglob("page-001.xml"))
    page.write_bytes(page.read_bytes() + "<!-- 변조 -->".encode())

    decision = await decide_reuse(owner_conn, store, "215971")

    assert isinstance(decision, DocumentSource)
    assert decision.source is FromDocumentSource.REFETCHED
    assert decision.skip_reason is ReuseSkipReason.SHA256_MISMATCH
