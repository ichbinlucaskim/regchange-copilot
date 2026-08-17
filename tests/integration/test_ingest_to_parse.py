"""수집부터 파싱까지 한 응답을 끝까지 처리한다 (작업 1 + 작업 2 결합).

이 파일이 존재하는 이유: 두 계층이 각각 통과해도 경계에서 어긋날 수 있다. 특히
`classify`가 마스킹한 문자열을 `parse_law_document`가 파싱할 수 있는지, 그리고
수집한 스냅샷을 저장·복원해도 파싱 결과가 같은지는 결합해 봐야만 알 수 있다.

네트워크를 쓰지 않는다. `MockTransport`가 픽스처를 응답으로 돌려준다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.ingest.canary import CANARY_DATE, IngestRunStatus, run_daily_ingest
from regchange.ingest.client import Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.response import LAW_DOCUMENT
from regchange.ingest.snapshot import new_run_id, read_pages, write_snapshot
from regchange.parse import parse_law_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "integration-test-oc"


def build_client(handler: object) -> LawApiClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)


async def test_document_is_collected_stored_and_parsed_end_to_end(tmp_path: Path) -> None:
    """본문을 수집 → 스냅샷 저장 → 복원 → 파싱까지 끝까지 처리한다."""
    body = (FIXTURES / "law_009244_mst252787.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["target"] == "law"
        assert request.url.params["MST"] == "252787"
        assert request.url.params["OC"] == OC
        return httpx.Response(200, content=body)

    client = build_client(handler)
    outcome = await client.collect(LAW_DOCUMENT, {"MST": "252787"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    # 본문 계열은 완주 검사 대상이 아니고, 조문단위 34건이 그대로 왔다.
    assert outcome.total_count is None
    assert len(outcome.items) == 34

    # 스냅샷을 페이지별로 저장하고 매니페스트로 되읽는다. 병합하지 않는다.
    store = LocalDocumentStore(tmp_path / "snapshots")
    run_id = new_run_id(datetime(2026, 8, 12, 7, 0, tzinfo=UTC), entropy="a3f1")
    manifest = await write_snapshot(
        store,
        outcome,
        run_id=run_id,
        fetched_at=datetime(2026, 8, 12, 7, 0, tzinfo=UTC),
        params={"MST": "252787"},
        display=None,
        masker=Masker(OC),
    )
    assert len(manifest.pages) == 1
    assert manifest.total_count is None  # 본문 계열은 완주 검사 대상이 아니다
    assert manifest.complete is None  # "검사 안 함"과 "실패"를 구별한다

    restored_pages = [page async for page in read_pages(store, manifest)]
    assert len(restored_pages) == 1
    snapshot = outcome.bodies[0]
    restored = restored_pages[0].decode("utf-8")
    assert restored == snapshot  # 저장한 페이지가 원본 그대로다

    # 파서가 수집 결과와 복원된 스냅샷에서 같은 트리를 만든다.
    from_wire = parse_law_document(snapshot)
    from_disk = parse_law_document(restored)

    # **수집한 조문단위 수와 파서의 unit 수가 일치한다.** 이것이 두 계층의 경계
    # 대조다 — 수집이 34건을 받았는데 파서가 그보다 적게 만들면 조용한 누락이다.
    assert len(from_wire.units) == len(outcome.items) == 34
    assert len(from_disk.units) == len(from_wire.units)

    # `articles`는 제목행을 제외한 조문 본체다 (ADR-001). units 보다 적은 것이 정상.
    assert 0 < len(from_wire.articles) < len(from_wire.units)
    assert len(from_wire.articles) == len(from_disk.articles)

    # 조문키 재구성이 원본과 일치한다 (작업 1의 채점 기준).
    for unit in from_wire.units:
        assert unit.article_key == unit.reconstructed_key()


async def test_masked_body_is_still_parseable_and_carries_no_credential() -> None:
    """마스킹이 파싱보다 먼저 일어나므로 트리에 자격증명이 남지 않는다."""
    raw = (FIXTURES / "law_010199_mst280277.xml").read_text(encoding="utf-8")
    # 응답에 OC가 echo된 상황을 만든다 (edge-case #1).
    injected = raw.replace("<법령키>", f"<링크>?OC={OC}&amp;t=1</링크><법령키>", 1)

    client = build_client(lambda request: httpx.Response(200, content=injected.encode("utf-8")))  # noqa: ARG005
    outcome = await client.collect(LAW_DOCUMENT, {"MST": "280277"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")

    assert OC not in outcome.bodies[0]
    document = parse_law_document(outcome.bodies[0])
    assert len(document.units) == len(outcome.items) == 80
    # 마스킹된 트리 어디에도 자격증명이 없다 — 참고자료·링크 필드까지 확인한다.
    assert OC not in repr(document)


async def test_daily_history_ingest_feeds_article_level_effective_dates() -> None:
    """일자별 이력 수집이 조문별 시행일을 조문 단위로 보존한다 (ADR-005 근거 2)."""
    canary = (FIXTURES / "dayjochg_regdt20250401.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["target"] == "lsJoHstInf"
        return httpx.Response(200, content=canary)

    result = await run_daily_ingest(build_client(handler), "20250401")
    assert result.status is IngestRunStatus.SUCCEEDED
    assert result.canary is not None and result.canary.passed
    assert result.collection is not None

    collection = result.collection
    assert collection.report.item_count == 83
    assert collection.report.article_count == 286

    # 문서 시행일자와 다른 조문시행일이 실제로 보존됐다 — 평탄화되지 않았다.
    effective_dates = {
        (
            item.findtext("법령정보/시행일자"),
            article.findtext("조문시행일"),
        )
        for item in collection.items
        for article in item.findall("조문정보/jo")
    }
    diverging = {pair for pair in effective_dates if pair[0] != pair[1]}
    assert diverging, "조문별 시행일 분기가 보존되지 않았다 (ADR-005 근거 2)"


async def test_canary_runs_before_the_main_collection_in_the_integrated_path() -> None:
    """통합 경로에서도 카나리아가 먼저 나간다."""
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.url.params.get("regDt", ""))
        return httpx.Response(200, content=(FIXTURES / "dayjochg_regdt20250401.xml").read_bytes())

    await run_daily_ingest(build_client(handler), "20260812")
    assert order[0] == CANARY_DATE
    assert order[1] == "20260812"
