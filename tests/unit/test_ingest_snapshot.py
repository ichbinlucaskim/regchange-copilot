"""스냅샷이 원본성을 지키는지 고정한다 — 병합 금지, sha256 검증, OC 차단.

이 파일이 존재하는 이유: 페이지를 병합하면 `totalCnt`와 행 수가 어긋난 문서가
생기고, 그 어긋남은 잘린 응답을 판별하는 유일한 단서다 (edge-case #11).
우리가 그런 문서를 만들면 스스로 위조 증거를 생산하는 것이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.client import PAGE_SIZE, Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import (
    JO_HISTORY_BY_ARTICLE,
    JO_HISTORY_BY_DATE,
    LAW_DOCUMENT,
    LAW_SEARCH,
)
from regchange.ingest.snapshot import (
    MANIFEST_FILENAME,
    Manifest,
    SnapshotError,
    build_manifest,
    count_target_of,
    encode_key,
    new_run_id,
    read_pages,
    semantic_params,
    utc_now,
    write_snapshot,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "snapshot-test-oc"
FETCHED_AT = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
RUN_ID = new_run_id(FETCHED_AT, entropy="a3f1")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def collect(name: str, spec: object, params: dict[str, str]) -> Collection:
    """픽스처를 응답으로 돌려주는 클라이언트로 한 번 수집한다."""
    body = fixture(name)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))  # noqa: ARG005
    )
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(spec, params)  # type: ignore[arg-type]
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")
    return outcome


# ---------------------------------------------------------------------------
# 1. run_id — UTC, 사전순 = 시간순
# ---------------------------------------------------------------------------


def test_run_id_is_utc_and_sorts_chronologically() -> None:
    earlier = new_run_id(datetime(2026, 8, 12, 7, 0, tzinfo=UTC), entropy="ffff")
    later = new_run_id(datetime(2026, 8, 12, 8, 0, tzinfo=UTC), entropy="0000")
    assert earlier < later  # 난수가 역순이어도 시간순이 이긴다
    assert earlier == "20260812T070000Z-ffff"


def test_run_id_converts_other_timezones_to_utc() -> None:
    """KST 시계로 만들어도 같은 순간이면 같은 run_id 다."""
    kst = timezone(timedelta(hours=9))
    from_utc = new_run_id(datetime(2026, 8, 12, 7, 0, tzinfo=UTC), entropy="abcd")
    from_kst = new_run_id(datetime(2026, 8, 12, 16, 0, tzinfo=kst), entropy="abcd")
    assert from_utc == from_kst


def test_naive_datetime_is_refused_for_run_id() -> None:
    with pytest.raises(SnapshotError, match="naive"):
        new_run_id(datetime(2026, 8, 12, 7, 0))  # noqa: DTZ001


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is not None


async def test_malformed_run_id_is_refused() -> None:
    """형식이 갈리면 사전순 정렬이 시간순과 어긋난다."""
    collection = await collect("law_009244_mst252787.xml", LAW_DOCUMENT, {"MST": "252787"})
    with pytest.raises(SnapshotError, match="run_id 형식"):
        build_manifest(
            collection,
            run_id="2026-08-12-run",
            fetched_at=FETCHED_AT,
            params={"MST": "252787"},
            display=None,
        )


# ---------------------------------------------------------------------------
# 2. 허용 목록 — OC 는 구조적으로 들어갈 수 없다
# ---------------------------------------------------------------------------


def test_oc_is_not_in_any_allowlist() -> None:
    """마스킹을 통과시키는 것이 아니라 애초에 목록에 없어서 들어갈 수 없다."""
    for spec in (LAW_SEARCH, LAW_DOCUMENT, JO_HISTORY_BY_DATE, JO_HISTORY_BY_ARTICLE):
        assert "OC" not in spec.semantic_params
        assert "type" not in spec.semantic_params
        assert "display" not in spec.semantic_params
        assert "page" not in spec.semantic_params


def test_params_outside_the_allowlist_raise() -> None:
    with pytest.raises(SnapshotError, match="허용 목록에 없는 파라미터"):
        semantic_params(JO_HISTORY_BY_DATE, {"regDt": "20260113", "OC": OC})


def test_display_in_params_is_refused() -> None:
    """페이지네이션은 같은 요청의 수행 방식이지 요청 자체가 아니다."""
    with pytest.raises(SnapshotError, match="display"):
        semantic_params(JO_HISTORY_BY_DATE, {"regDt": "20260113", "display": "100"})


async def test_manifest_never_contains_the_credential(tmp_path: Path) -> None:
    """매니페스트 전체 문자열에 OC 가 없다."""
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    store = LocalDocumentStore(tmp_path / "s")
    manifest = await write_snapshot(
        store,
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
        masker=Masker(OC),
    )
    raw = (await store.get(f"{manifest.directory}/{MANIFEST_FILENAME}")).decode("utf-8")
    assert OC not in raw
    assert OC not in manifest.to_json()
    assert "OC" not in manifest.params


# ---------------------------------------------------------------------------
# 3. 디렉터리 키 — 정규화 + 해시
# ---------------------------------------------------------------------------


def test_key_is_readable_and_carries_a_hash_suffix() -> None:
    key = encode_key(JO_HISTORY_BY_DATE, {"regDt": "20260113"})
    assert key.startswith("regDt-20260113-")
    assert len(key.split("-")[-1]) == 6


def test_keys_that_normalize_to_the_same_string_do_not_collide() -> None:
    """정규화만으로는 붕괴한다. 해시가 그것을 막는다."""
    first = encode_key(LAW_SEARCH, {"query": "가나"})
    second = encode_key(LAW_SEARCH, {"query": "다라"})
    # 읽을 수 있는 부분은 같아진다.
    assert first.rsplit("-", 1)[0] == second.rsplit("-", 1)[0]
    # 그러나 해시가 다르므로 디렉터리가 붕괴하지 않는다.
    assert first != second


def test_key_does_not_depend_on_dict_iteration_order() -> None:
    forward = encode_key(JO_HISTORY_BY_ARTICLE, {"ID": "009244", "JO": "000200"})
    backward = encode_key(JO_HISTORY_BY_ARTICLE, {"JO": "000200", "ID": "009244"})
    assert forward == backward


def test_key_contains_no_unsafe_characters() -> None:
    key = encode_key(LAW_SEARCH, {"query": "은행법 시행령/부칙"})
    assert all(char.isalnum() or char in "._=-" for char in key), key


# ---------------------------------------------------------------------------
# 4. 계열별 count_target 과 null 구분
# ---------------------------------------------------------------------------


def test_count_target_differs_by_family() -> None:
    assert count_target_of(JO_HISTORY_BY_DATE) == "law_elements"
    assert count_target_of(JO_HISTORY_BY_ARTICLE) == "law_elements"
    assert count_target_of(LAW_SEARCH) == "item_elements"
    assert count_target_of(LAW_DOCUMENT) == "none"


async def test_document_family_records_null_not_false() -> None:
    """'검사 안 함'과 '검사했고 실패'를 구별한다. false 로 두면 섞인다."""
    collection = await collect("law_009244_mst252787.xml", LAW_DOCUMENT, {"MST": "252787"})
    manifest = build_manifest(
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"MST": "252787"},
        display=None,
    )
    assert manifest.total_count is None
    assert manifest.complete is None
    assert manifest.complete is not False  # 명시적으로 구별한다
    assert manifest.count_target == "none"
    assert manifest.display is None
    assert manifest.received == 34


async def test_history_family_records_complete_true_with_count_target() -> None:
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    manifest = build_manifest(
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
    )
    assert manifest.total_count == 83
    assert manifest.received == 83  # 법령 수. 조문 286건이 아니다
    assert manifest.complete is True
    assert manifest.count_target == "law_elements"
    assert manifest.display == PAGE_SIZE


async def test_manifest_json_round_trips() -> None:
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    manifest = build_manifest(
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
    )
    assert Manifest.from_json(manifest.to_json()) == manifest
    assert manifest.fetched_at == "2026-08-12T07:00:00+00:00"


# ---------------------------------------------------------------------------
# 5. 미지의 열거값이 매니페스트에 남는다
# ---------------------------------------------------------------------------


async def test_unknown_enum_values_are_recorded_in_the_manifest() -> None:
    """로그에만 남기면 나중에 못 찾는다."""
    body = (
        '<LawSearch><target>lsJoHstInf</target><totalCnt>1</totalCnt><law id="1">'
        "<법령정보><법령일련번호>1</법령일련번호><법령ID>x</법령ID></법령정보>"
        "<조문정보><jo num='1'><조문번호>000200</조문번호>"
        "<변경사유>새로운사유</변경사유><조문시행일>20250401</조문시행일>"
        "</jo></조문정보></law></LawSearch>"
    ).encode()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))  # noqa: ARG005
    )
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, Collection)

    manifest = build_manifest(
        outcome,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
    )
    assert manifest.unknown_enum_values == ({"field": "변경사유", "values": ["새로운사유"]},)
    # JSON 왕복 후에도 남는다.
    assert "새로운사유" in manifest.to_json()


async def test_key_conflicts_records_zero_when_checked() -> None:
    """'검사했고 0이었다'와 '검사가 돌지 않아 0이다'를 구별하기 위한 필드다."""
    collection = await collect(
        "jochg_009244_jo000200_full.xml", JO_HISTORY_BY_ARTICLE, {"ID": "009244", "JO": "000200"}
    )
    manifest = build_manifest(
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"ID": "009244", "JO": "000200"},
        display=PAGE_SIZE,
    )
    assert manifest.key_conflicts == 0
    assert collection.report.checked_keys == 28  # 실제로 28건을 검사했다


# ---------------------------------------------------------------------------
# 6. 저장과 되읽기 — 원본 그대로, sha256 검증
# ---------------------------------------------------------------------------


async def test_pages_are_stored_verbatim_and_are_valid_xml(tmp_path: Path) -> None:
    """저장한 페이지가 그 자체로 적격 XML 이다 — 병합하지 않았다는 증거."""
    from defusedxml.ElementTree import fromstring

    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    store = LocalDocumentStore(tmp_path / "s")
    manifest = await write_snapshot(
        store,
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
        masker=Masker(OC),
    )
    pages = [page async for page in read_pages(store, manifest)]
    assert len(pages) == 1
    root = fromstring(pages[0])
    assert root.tag == "LawSearch"
    # totalCnt 와 실제 행 수가 어긋나지 않는다 — 우리가 만든 문서가 아니므로.
    assert int(root.findtext("totalCnt") or 0) == len(root.findall("law")) == 83


async def test_read_pages_detects_a_tampered_file(tmp_path: Path) -> None:
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    root = tmp_path / "s"
    store = LocalDocumentStore(root)
    manifest = await write_snapshot(
        store,
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
        masker=Masker(OC),
    )
    target = root / manifest.directory / manifest.pages[0].file
    target.write_bytes(target.read_bytes().replace(b"<totalCnt>83", b"<totalCnt>82"))

    with pytest.raises(SnapshotError, match="sha256 불일치"):
        [page async for page in read_pages(store, manifest)]


async def test_read_pages_fails_when_a_page_file_is_missing(tmp_path: Path) -> None:
    """건너뛰면 부분 데이터가 정상으로 보인다."""
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    root = tmp_path / "s"
    store = LocalDocumentStore(root)
    manifest = await write_snapshot(
        store,
        collection,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20250401"},
        display=PAGE_SIZE,
        masker=Masker(OC),
    )
    (root / manifest.directory / manifest.pages[0].file).unlink()

    with pytest.raises(SnapshotError, match="페이지 파일이 없다"):
        [page async for page in read_pages(store, manifest)]


async def test_rewriting_the_same_key_is_refused(tmp_path: Path) -> None:
    """해시 충돌이든 재수집이든 덮어쓰지 않는다."""
    collection = await collect(
        "dayjochg_regdt20250401.xml", JO_HISTORY_BY_DATE, {"regDt": "20250401"}
    )
    store = LocalDocumentStore(tmp_path / "s")
    kwargs = {
        "run_id": RUN_ID,
        "fetched_at": FETCHED_AT,
        "params": {"regDt": "20250401"},
        "display": PAGE_SIZE,
        "masker": Masker(OC),
    }
    await write_snapshot(store, collection, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(SnapshotError, match="이미 매니페스트가 있는"):
        await write_snapshot(store, collection, **kwargs)  # type: ignore[arg-type]


async def test_multi_page_snapshot_keeps_each_page_separate(tmp_path: Path) -> None:
    """581건을 6페이지로 받으면 파일이 6개이고 각각 적격 XML 이다."""
    from defusedxml.ElementTree import fromstring

    source = fixture("dayjochg_regdt20210105.xml")
    blocks: list[bytes] = []
    cursor = 0
    while (start := source.find(b"<law ", cursor)) != -1:
        end = source.find(b"</law>", start)
        blocks.append(source[start : end + 6])
        cursor = end
    header = b"<LawSearch><target>lsJoHstInf</target><totalCnt>581</totalCnt>"
    pages = [
        header + b"".join(blocks[offset : offset + PAGE_SIZE]) + b"</LawSearch>"
        for offset in range(0, 581, PAGE_SIZE)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pages[int(request.url.params.get("page", "1")) - 1])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20210105"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    store = LocalDocumentStore(tmp_path / "s")
    manifest = await write_snapshot(
        store,
        outcome,
        run_id=RUN_ID,
        fetched_at=FETCHED_AT,
        params={"regDt": "20210105"},
        display=PAGE_SIZE,
        masker=Masker(OC),
    )
    assert len(manifest.pages) == 6
    assert [page.items for page in manifest.pages] == [100, 100, 100, 100, 100, 81]
    assert manifest.received == 581
    assert manifest.complete is True

    restored = [page async for page in read_pages(store, manifest)]
    assert len(restored) == 6
    # 각 페이지가 독립적으로 파싱된다. 합쳐진 문서가 존재하지 않는다.
    assert sum(len(fromstring(page).findall("law")) for page in restored) == 581
