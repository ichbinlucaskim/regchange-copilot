"""스로틀·재시도·완주·카나리아가 정책대로 동작하는지 고정한다.

이 파일이 존재하는 이유: 수집 경로의 실패는 조용하다. 잘린 응답을 성공으로 처리하거나
카나리아 실패 후에도 수집을 진행하면 "그날 개정 없음"으로 위장한다 (R-11, ADR-005).
네트워크 없이 `MockTransport`로 전부 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from regchange.ingest.canary import (
    CANARY_DATE,
    CANARY_EXPECTED_COUNT,
    DailyIngestResult,
    IngestRunStatus,
    confirm_zero,
    probe_canary,
    run_daily_ingest,
)
from regchange.ingest.client import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    MIN_CALL_INTERVAL_SECONDS,
    PAGE_SIZE,
    Collection,
    CollectionFailure,
    CollectionFailureReason,
    LawApiClient,
)
from regchange.ingest.masking import MASK_PLACEHOLDER, MaskingError
from regchange.ingest.response import (
    JO_HISTORY_BY_ARTICLE,
    JO_HISTORY_BY_DATE,
    LAW_DOCUMENT,
    ResponseKind,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
BASE_URL = "https://www.law.go.kr/DRF"
OC = "test-oc-value"

ZERO_BODY = b"<LawSearch><target>lsJoHstInf</target><totalCnt>0</totalCnt></LawSearch>"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def build_client(
    handler: object, *, sleeps: list[float] | None = None, oc: str = OC
) -> LawApiClient:
    """MockTransport 로 클라이언트를 만든다. 대기는 기록만 하고 실제로 자지 않는다."""
    recorded = sleeps if sleeps is not None else []
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.AsyncClient(transport=transport)
    return LawApiClient(
        BASE_URL,
        http,
        oc,
        sleep=recorded.append,
        jitter=lambda: 1.0,
    )


def always(body: bytes) -> object:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=body)

    return handler


# ---------------------------------------------------------------------------
# 1. 스로틀 — 호출 간 최소 간격
# ---------------------------------------------------------------------------


async def test_first_call_does_not_wait_and_later_calls_do() -> None:
    sleeps: list[float] = []
    client = build_client(always(fixture("dayjochg_regdt20250401.xml")), sleeps=sleeps)
    await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert sleeps == []  # 단일 페이지 = 1회 호출. 첫 호출은 대기하지 않는다

    await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= MIN_CALL_INTERVAL_SECONDS


async def test_page_size_is_100_not_the_undocumented_1000() -> None:
    """미문서화 동작(display=1000)에 의존하지 않는다."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("display", ""))
        return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))

    client = build_client(handler)
    await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert seen == [str(PAGE_SIZE)] == ["100"]


async def test_document_family_gets_no_display_or_page_param() -> None:
    """본문 계열은 페이지네이션이 없다."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=fixture("law_009244_mst252787.xml"))

    client = build_client(handler)
    outcome = await client.collect(LAW_DOCUMENT, {"MST": "252787"})
    assert isinstance(outcome, Collection)
    assert len(seen) == 1
    assert "display" not in seen[0].params
    assert "page" not in seen[0].params


# ---------------------------------------------------------------------------
# 2. 재시도 — 네트워크 오류에만, 백오프와 로그
# ---------------------------------------------------------------------------


async def test_transport_error_is_retried_then_succeeds() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))

    client = build_client(handler, sleeps=sleeps)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, Collection)
    assert outcome.stats.retries == 1
    assert outcome.stats.requests == 2
    record = outcome.stats.records[0]
    assert record.attempt == 1
    assert record.error_type == "ConnectError"
    assert record.waited_seconds == pytest.approx(BACKOFF_BASE_SECONDS)


async def test_backoff_doubles_and_starts_at_the_min_interval() -> None:
    """백오프 시작값이 최소 호출 간격과 같고 2배씩 늘어난다."""
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ReadTimeout("slow")

    client = build_client(handler, sleeps=sleeps)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.TRANSPORT
    assert outcome.stats.requests == MAX_ATTEMPTS
    assert outcome.stats.retries == MAX_ATTEMPTS - 1

    waited = [record.waited_seconds for record in outcome.stats.records]
    assert waited == pytest.approx([BACKOFF_BASE_SECONDS, BACKOFF_BASE_SECONDS * 2])
    assert BACKOFF_BASE_SECONDS == MIN_CALL_INTERVAL_SECONDS  # 우연이 아니라 의도다


async def test_backoff_is_layered_on_top_of_the_throttle_not_instead_of_it() -> None:
    """백오프 대기는 최소 간격을 대체하지 않고 그 위에 얹힌다."""
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectError("boom")

    client = build_client(handler, sleeps=sleeps)
    await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    # 시도 3회 = 스로틀 2회(2·3번째 호출) + 백오프 2회
    assert len(sleeps) == 4


async def test_response_shape_failure_is_not_retried() -> None:
    """파라미터가 틀린 것이므로 재시도해도 같다."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, content=fixture("error_target_lsHistory.html"))

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.RESPONSE_SHAPE
    assert outcome.response_kind is ResponseKind.HTML
    assert calls["n"] == 1  # 한 번만 호출했다
    assert outcome.stats.retries == 0


async def test_http_status_is_not_used_to_detect_failure() -> None:
    """실패도 200으로 오므로 상태코드는 아무것도 구별하지 못한다 (edge-case #10)."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=fixture("error_unknown_target_empty.xml"))

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.response_kind is ResponseKind.EMPTY_BODY


# ---------------------------------------------------------------------------
# 3. 요청 전 파라미터 검증
# ---------------------------------------------------------------------------


async def test_missing_required_param_is_rejected_before_sending() -> None:
    """이력 계열의 파라미터 오류는 응답으로 구별 불가능하므로 보내기 전에 막는다."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, content=ZERO_BODY)

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"ID": "009244"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.BAD_REQUEST
    assert "regDt" in outcome.detail
    assert calls["n"] == 0  # 네트워크를 쓰지 않았다


async def test_empty_param_value_counts_as_missing() -> None:
    client = build_client(always(ZERO_BODY))
    assert client.missing_params(JO_HISTORY_BY_DATE, {"regDt": "  "}) == frozenset({"regDt"})
    assert client.missing_params(JO_HISTORY_BY_ARTICLE, {"ID": "009244"}) == frozenset({"JO"})


# ---------------------------------------------------------------------------
# 4. 완주 — 잘린 응답을 실패로 처리한다
# ---------------------------------------------------------------------------


async def test_truncated_single_page_response_fails() -> None:
    """totalCnt=28인데 20건만 오고 2페이지가 0건이면 진전 없음으로 실패한다."""
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(200, content=fixture("jochg_009244_jo000200.xml"))
        return httpx.Response(
            200,
            content=b"<LawService><target>lsJoHstInf</target><totalCnt>28</totalCnt></LawService>",
        )

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_ARTICLE, {"ID": "009244", "JO": "000200"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.NO_PROGRESS
    assert outcome.expected == 28
    assert outcome.received == 20


def _law_blocks(body: bytes) -> list[bytes]:
    """`<law ...>...</law>` 블록을 순서대로 잘라낸다 (테스트용 페이지 조립)."""
    blocks: list[bytes] = []
    cursor = 0
    while True:
        start = body.find(b"<law ", cursor)
        if start == -1:
            return blocks
        end = body.find(b"</law>", start)
        blocks.append(body[start : end + len(b"</law>")])
        cursor = end


def _paged_bodies(name: str, total: int, size: int) -> list[bytes]:
    """실제 픽스처를 `size` 건씩 잘라 여러 페이지 응답으로 만든다."""
    blocks = _law_blocks(fixture(name))
    assert len(blocks) == total, f"{name}: 블록 {len(blocks)}건 (기대 {total})"
    header = f"<LawSearch><target>lsJoHstInf</target><totalCnt>{total}</totalCnt>".encode()
    return [
        header + b"".join(blocks[offset : offset + size]) + b"</LawSearch>"
        for offset in range(0, total, size)
    ]


async def test_pagination_completes_across_six_real_pages() -> None:
    """581건을 100건씩 6페이지로 완주한다 (실제 픽스처를 잘라 만든 응답)."""
    pages = _paged_bodies("dayjochg_regdt20210105.xml", 581, PAGE_SIZE)
    assert [len(_law_blocks(page)) for page in pages] == [100, 100, 100, 100, 100, 81]
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        seen.append(page)
        return httpx.Response(200, content=pages[page - 1])

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20210105"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")
    assert seen == [1, 2, 3, 4, 5, 6]
    assert len(outcome.items) == 581
    assert outcome.report.ok
    assert outcome.report.checked_keys == 2335  # 조문 전건이 보존됐다
    assert len(outcome.bodies) == 6


async def test_repeated_identical_page_is_rejected_as_no_progress() -> None:
    """같은 페이지를 반복해서 주는 응답이 "진전"으로 인정되면 안 된다.

    구현 중 실측으로 발견한 결함이다. 항목 0건 검사만 있으면 1건짜리 페이지가
    반복될 때 매번 1건씩 늘어나 검사를 통과하고, 같은 항목이 totalCnt 만큼
    누적된 뒤 완주로 보인다.
    """
    block = _law_blocks(fixture("dayjochg_regdt20250401.xml"))[0]
    body = (
        b"<LawSearch><target>lsJoHstInf</target><totalCnt>84</totalCnt>" + block + b"</LawSearch>"
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, content=body)

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.NO_PROGRESS
    assert "덜 찼으므로" in outcome.detail
    assert calls["n"] == 2  # 2페이지에서 즉시 멈춘다. 84번 돌지 않는다


async def test_short_non_final_page_fails_even_when_items_are_new() -> None:
    """새 항목이 와도 페이지가 덜 찼으면 실패다 — API가 더 주지 않는다는 뜻이다."""
    blocks = _law_blocks(fixture("dayjochg_regdt20210105.xml"))
    header = b"<LawSearch><target>lsJoHstInf</target><totalCnt>581</totalCnt>"
    pages = [
        header + b"".join(blocks[:100]) + b"</LawSearch>",
        header + b"".join(blocks[100:150]) + b"</LawSearch>",  # 50건만 = 덜 찬 페이지
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pages[int(request.url.params.get("page", "1")) - 1])

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20210105"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.NO_PROGRESS
    assert outcome.received == 150
    assert outcome.expected == 581


async def test_shape_failure_mid_pagination_does_not_return_partial_data() -> None:
    """중간 페이지 실패 시 받은 페이지를 병합해 돌려주지 않는다."""
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(200, content=fixture("jochg_009244_jo000200.xml"))
        return httpx.Response(200, content=fixture("error_law_bad_mst.xml"))

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_ARTICLE, {"ID": "009244", "JO": "000200"})
    assert isinstance(outcome, CollectionFailure)
    assert outcome.reason is CollectionFailureReason.RESPONSE_SHAPE
    assert outcome.response_kind is ResponseKind.LAW_MESSAGE
    assert outcome.received == 20  # 진단 숫자는 남지만
    assert not hasattr(outcome, "items")  # 데이터는 담기지 않는다


async def test_complete_response_passes_and_records_integrity() -> None:
    client = build_client(always(fixture("dayjochg_regdt20250401.xml")))
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, Collection)
    assert outcome.report.ok
    assert outcome.report.item_count == 83
    assert outcome.report.article_count == 286
    assert outcome.report.checked_keys == 286


# ---------------------------------------------------------------------------
# 5. 마스킹 — 저장·로깅 경로 전체
# ---------------------------------------------------------------------------


async def test_oc_is_sent_and_echoed_value_is_masked() -> None:
    """보내는 OC와 마스킹하는 OC가 같은 값이다."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("OC"))
        body = (
            f'<LawSearch><target>lsJoHstInf</target><totalCnt>1</totalCnt><law id="1">'
            f"<법령정보><법령일련번호>1</법령일련번호><법령ID>x</법령ID></법령정보>"
            f"<조문정보><jo num='1'><조문번호>000200</조문번호><변경사유>조문변경</변경사유>"
            f"<조문링크>/DRF/lawService.do?OC={OC}&amp;target=eflaw</조문링크>"
            f"<조문시행일>20250401</조문시행일></jo></조문정보></law></LawSearch>"
        ).encode()
        return httpx.Response(200, content=body)

    client = build_client(handler)
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")
    assert seen == [OC]  # 요청에 실렸다
    assert OC not in outcome.bodies[0]  # 저장용 본문에는 없다
    assert MASK_PLACEHOLDER in outcome.bodies[0]


async def test_client_rejects_empty_oc_at_construction() -> None:
    """빈 OC로는 클라이언트를 만들 수 없다."""
    with pytest.raises(MaskingError):
        build_client(always(ZERO_BODY), oc="")


# ---------------------------------------------------------------------------
# 6. 미지의 열거값 — 기록하고 수집은 성공
# ---------------------------------------------------------------------------


async def test_unknown_enum_value_is_recorded_but_collection_succeeds() -> None:
    body = (
        '<LawSearch><target>lsJoHstInf</target><totalCnt>1</totalCnt><law id="1">'
        "<법령정보><법령일련번호>1</법령일련번호><법령ID>x</법령ID></법령정보>"
        "<조문정보><jo num='1'><조문번호>000200</조문번호>"
        "<변경사유>미래에생길새로운사유</변경사유>"
        "<조문시행일>20250401</조문시행일></jo></조문정보></law></LawSearch>"
    ).encode()
    client = build_client(always(body))
    outcome = await client.collect(JO_HISTORY_BY_DATE, {"regDt": "20250401"})
    assert isinstance(outcome, Collection), getattr(outcome, "detail", "")
    assert outcome.unknown_enum_values == {"변경사유": ("미래에생길새로운사유",)}


# ---------------------------------------------------------------------------
# 7. 카나리아 — 실패 시 본 수집이 실행되지 않는다
# ---------------------------------------------------------------------------


async def test_canary_passes_on_known_non_zero_date() -> None:
    client = build_client(always(fixture("dayjochg_regdt20250401.xml")))
    result = await probe_canary(client)
    assert result.passed
    assert result.total_count == CANARY_EXPECTED_COUNT
    assert not result.drifted


async def test_canary_fails_when_known_date_returns_zero() -> None:
    client = build_client(always(ZERO_BODY))
    result = await probe_canary(client)
    assert not result.passed
    assert result.total_count == 0


async def test_canary_drift_warns_but_passes() -> None:
    """83에서 달라지면 경고만 남기고 통과시킨다 — 법제처 DB 갱신일 수 있다."""
    blocks = _law_blocks(fixture("dayjochg_regdt20250401.xml"))
    assert len(blocks) == 83
    # 83건에서 하나 줄어든(82건) 정상 응답을 만든다. 완주는 맞고 기준값만 다르다.
    body = (
        b"<LawSearch><target>lsJoHstInf</target><totalCnt>82</totalCnt>"
        + b"".join(blocks[:82])
        + b"</LawSearch>"
    )
    result = await probe_canary(build_client(always(body)))
    assert result.passed  # 실패로 처리하지 않는다
    assert result.drifted
    assert result.total_count == 82 != CANARY_EXPECTED_COUNT


async def test_main_collection_does_not_run_when_canary_fails() -> None:
    """카나리아 실패 시 본 수집 요청이 아예 나가지 않는다."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params.get("regDt", ""))
        return httpx.Response(200, content=ZERO_BODY)  # 카나리아 날짜도 0건 = 실패

    client = build_client(handler)
    result = await run_daily_ingest(client, "20260812")
    assert result.status is IngestRunStatus.SKIPPED_CANARY_FAILED
    assert result.collection is None
    assert requests == [CANARY_DATE]  # 카나리아만 호출했고 본 수집은 하지 않았다


async def test_skipped_status_is_distinct_from_failed() -> None:
    """'실패했다'와 '안 했다'가 다른 값이다 — 실패율 지표에 섞이면 안 된다."""
    # 다섯 상태가 서로 다른 값이다. 개별 쌍을 비교하면 mypy 가 리터럴로 좁혀
    # "겹치지 않는 비교"로 잡으므로, 집합 크기로 확인한다.
    assert len({status.value for status in IngestRunStatus}) == 5
    # 미수행도 실패도 "수집된 상태"가 아니다. 0건 성공은 수집된 상태다.
    assert not DailyIngestResult(status=IngestRunStatus.SKIPPED_CANARY_FAILED, detail="").collected
    assert not DailyIngestResult(status=IngestRunStatus.FAILED, detail="").collected
    assert DailyIngestResult(status=IngestRunStatus.SUCCEEDED_ZERO, detail="").collected


# ---------------------------------------------------------------------------
# 8. 0건 재요청
# ---------------------------------------------------------------------------


async def test_zero_is_confirmed_by_a_second_request() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.params.get("regDt") == CANARY_DATE:
            return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))
        return httpx.Response(200, content=ZERO_BODY)

    client = build_client(handler)
    result = await run_daily_ingest(client, "20260812")
    assert result.status is IngestRunStatus.SUCCEEDED_ZERO
    assert result.zero_confirmation is not None
    assert result.zero_confirmation.confirmed
    assert calls["n"] == 3  # 카나리아 + 본 수집 + 재요청


async def test_zero_recheck_mismatch_is_a_failure_not_a_warning() -> None:
    """첫 요청 0, 재요청 비-0이면 실패다. 첫 요청이 조용히 적게 셌다는 뜻이다."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.params.get("regDt") == CANARY_DATE:
            return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))
        if calls["n"] == 2:
            return httpx.Response(200, content=ZERO_BODY)
        return httpx.Response(200, content=fixture("dayjochg_regdt20260113.xml"))

    client = build_client(handler)
    result = await run_daily_ingest(client, "20260113")
    assert result.status is IngestRunStatus.FAILED_ZERO_UNCONFIRMED
    assert result.collection is None  # 0으로 기록하지 않는다
    assert result.zero_confirmation is not None
    assert result.zero_confirmation.is_incident_candidate
    assert "사건" not in result.detail or "후보" in result.detail


async def test_zero_recheck_transport_failure_blocks_zero_record() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.params.get("regDt") == CANARY_DATE:
            return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))
        if calls["n"] == 2:
            return httpx.Response(200, content=ZERO_BODY)
        raise httpx.ConnectError("down")

    client = build_client(handler)
    result = await run_daily_ingest(client, "20260812")
    assert result.status is IngestRunStatus.FAILED_ZERO_UNCONFIRMED
    assert result.zero_confirmation is not None
    assert result.zero_confirmation.second_count is None
    assert not result.zero_confirmation.is_incident_candidate  # 실패는 불일치가 아니다


async def test_non_zero_collection_does_not_trigger_a_recheck() -> None:
    """비-0은 요청이 제대로 갔다는 증거 자체다."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(200, content=fixture("dayjochg_regdt20250401.xml"))

    client = build_client(handler)
    result = await run_daily_ingest(client, "20250401")
    assert result.status is IngestRunStatus.SUCCEEDED
    assert result.zero_confirmation is None
    assert calls["n"] == 2  # 카나리아 + 본 수집만


async def test_confirm_zero_reports_the_second_count_for_incident_records() -> None:
    client = build_client(always(fixture("dayjochg_regdt20260113.xml")))
    confirmation = await confirm_zero(client, {"regDt": "20260113"})
    assert confirmation.second_count == 1
    assert not confirmation.confirmed
    assert confirmation.is_incident_candidate
