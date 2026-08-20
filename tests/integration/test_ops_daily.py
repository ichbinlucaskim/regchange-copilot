"""일일 작업 전체 경로 — 폴링에서 change_set 과 실행 이력까지.

이 테스트가 존재하는 이유: 운영 실적의 근거가 되는 경로이므로 **한 번도 안 돌려보고
cron 에 걸면 안 된다.** 특히 세 가지를 발화시킨다.

  1. 한 법령의 실패가 실행 전체를 실패시키지 않는가 (부분 실패)
  2. 같은 날짜를 다시 처리해도 중복이 생기지 않는가 (멱등성)
  3. 카나리아가 실패하면 폴링을 아예 하지 않는가 (0건으로 기록하지 않는다)

응답은 실측 픽스처로 만든다. `oldAndNew` 만 픽스처가 없어 최소 형태로 조립하는데,
**값은 전부 실측에서 온다** — MST 215971/113262, 법령ID 009244, 공포일자와 공포번호는
`law_009244_*.xml` 두 픽스처의 `기본정보` 그대로다. 형태는
`oldandnew_000030_mst285199.xml` 을 따른다.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from regchange.adapters.storage.local import LocalDocumentStore
from regchange.config.corpus import Corpus, CorpusConfig, LawRef
from regchange.ingest.client import LawApiClient
from regchange.ingest.masking import Masker
from regchange.ops import record_run, run_daily
from regchange.ops.models import LawOutcomeStatus, OpsRunStatus

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "law_api"
OC = "ops-daily-test-oc"
NOW = dt.datetime(2026, 8, 20, 22, 0, tzinfo=dt.UTC)

CANARY_DATE = "20250401"
TARGET_DATE = "20200324"
"""특금법(009244) MST 215971 이 실제로 공포된 날. `dayjochg_regdt20200324.xml` 에 있다."""

NEW_MST = "215971"
OLD_MST = "113262"

CORPUS = CorpusConfig(
    version=1,
    corpora=(
        Corpus(
            key="test",
            label="테스트",
            active=True,
            laws=(LawRef(law_id="009244", name="특정 금융거래정보의 보고 및 이용 등에 관한 법률"),),
        ),
    ),
)

OLD_AND_NEW = (
    '<?xml version="1.0" encoding="UTF-8"?><OldAndNewService>'
    "<구조문_기본정보>"
    f"<법령일련번호>{OLD_MST}</법령일련번호><법령ID>009244</법령ID>"
    "<시행일자>20110519</시행일자><공포일자>20110519</공포일자><공포번호>10694</공포번호>"
    "<현행여부>N</현행여부><제개정구분명>일부개정</제개정구분명>"
    "<법령명><![CDATA[특정 금융거래정보의 보고 및 이용 등에 관한 법률]]></법령명>"
    "</구조문_기본정보>"
    "<신조문_기본정보>"
    f"<법령일련번호>{NEW_MST}</법령일련번호><법령ID>009244</법령ID>"
    "<시행일자>20210325</시행일자><공포일자>20200324</공포일자><공포번호>17113</공포번호>"
    "<현행여부>N</현행여부><제개정구분명>일부개정</제개정구분명>"
    "<법령명><![CDATA[특정 금융거래정보의 보고 및 이용 등에 관한 법률]]></법령명>"
    "</신조문_기본정보>"
    "</OldAndNewService>"
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Api:
    """법제처 응답을 흉내 내는 라우터. 어떤 요청이 몇 번 갔는지 센다.

    호출 수를 세는 이유: 멱등성의 증거가 "결과가 같다"가 아니라 **"API 를 다시
    부르지 않았다"**이기 때문이다. 결과만 보면 다시 받아서 같은 답을 낸 경우와
    구별되지 않는다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.canary_ok = True
        self.old_and_new_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        target = params.get("target", [""])[0]
        key = params.get("MST", params.get("regDt", [""]))[0]
        self.calls.append((target, key))

        if target == "lsJoHstInf":
            if key == CANARY_DATE:
                if not self.canary_ok:
                    return httpx.Response(200, content=b"<html>service down</html>")
                return httpx.Response(200, content=_fixture("dayjochg_regdt20250401.xml"))
            if key == TARGET_DATE:
                return httpx.Response(200, content=_fixture("dayjochg_regdt20200324.xml"))
            return httpx.Response(200, content=_fixture("error_dayjochg_id_only_zero.xml"))

        if target == "oldAndNew":
            if not self.old_and_new_ok:
                return httpx.Response(200, content=_fixture("error_law_bad_mst.xml"))
            return httpx.Response(200, content=OLD_AND_NEW.encode("utf-8"))

        if target == "law":
            name = (
                "law_009244_mst215971_v20200324.xml"
                if key == NEW_MST
                else "law_009244_mst113262_v20110519.xml"
            )
            return httpx.Response(200, content=_fixture(name))

        raise AssertionError(f"예상하지 못한 target: {target}")

    def count(self, target: str) -> int:
        return sum(1 for call in self.calls if call[0] == target)


@pytest.fixture
def api() -> Api:
    return Api()


@pytest.fixture
def store(tmp_path: Path) -> LocalDocumentStore:
    return LocalDocumentStore(tmp_path / "snapshots")


async def _run(
    conn: psycopg.AsyncConnection[Any],
    api: Api,
    store: LocalDocumentStore,
    *,
    run_seed: str,
    dates: tuple[str, ...] = (TARGET_DATE,),
) -> Any:
    """일일 작업을 한 번 돌린다. `run_id` 는 실행마다 달라야 한다(스냅샷 디렉터리)."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        client = LawApiClient("https://www.law.go.kr/DRF", http, OC, sleep=lambda _: None)
        return await run_daily(
            conn,
            client,
            store,
            Masker(OC),
            corpus=CORPUS,
            dates=dates,
            run_id=f"20260820T220000Z-{run_seed}",
            now=NOW,
        )


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


async def test_daily_run_detects_the_corpus_law_and_makes_a_change_set(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """폴링 → 코퍼스 필터 → 직전 MST 확보 → 적재 → diff 가 한 번에 돈다."""
    result = await _run(owner_conn, api, store, run_seed="aaaa")

    assert result.status is OpsRunStatus.SUCCEEDED, result.detail
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status is LawOutcomeStatus.DIFFED
    assert outcome.detected.mst == NEW_MST
    assert outcome.from_mst == OLD_MST
    assert outcome.change_set_id is not None
    assert outcome.articles_changed is not None and outcome.articles_changed > 0

    # 194건 중 코퍼스는 1건이다. 전체 건수도 함께 남겨 "0건인 날"의 두 이유를 구별한다.
    probe = result.probes[0]
    assert probe.total_count == 194
    assert probe.matched == 1


async def test_the_polling_response_is_kept_when_the_day_produced_work(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore, tmp_path: Path
) -> None:
    """diff 를 만든 날의 근거 응답은 반드시 남는다 — "왜 이 비교를 했는가"의 출처다."""
    await _run(owner_conn, api, store, run_seed="bbbb")

    saved = list((tmp_path / "snapshots").rglob("manifest.json"))
    targets = {path.parent.parent.name for path in saved}
    assert "lsJoHstInf" in targets
    assert "oldAndNew" in targets
    assert "law" in targets


async def test_the_run_is_recorded_with_its_law_outcomes(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """실행 이력이 없으면 운영 실적을 주장할 수 없다."""
    result = await _run(owner_conn, api, store, run_seed="cccc")
    ops_run_id = await record_run(owner_conn, result)

    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM ops_run WHERE id = %s", (ops_run_id,))
        run = await cur.fetchone()
        await cur.execute("SELECT * FROM ops_law_outcome WHERE ops_run_id = %s", (ops_run_id,))
        outcomes = await cur.fetchall()

    assert run is not None
    assert run["status"] == OpsRunStatus.SUCCEEDED.value
    assert run["laws_detected"] == 1
    assert run["laws_diffed"] == 1
    assert run["change_sets_created"] == 1
    assert run["canary_passed"] is True
    assert run["target_dates"] == [TARGET_DATE]
    assert run["date_probes"][0]["total_count"] == 194
    assert len(outcomes) == 1
    assert outcomes[0]["mst"] == NEW_MST
    assert outcomes[0]["failure_detail"] is None


# ---------------------------------------------------------------------------
# 멱등성 — 최근 N일 재확인이 같은 일을 반복하지 않는다
# ---------------------------------------------------------------------------


async def test_second_run_of_the_same_date_does_not_call_the_api_again(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """**재확인 창의 전제.** 이미 처리한 MST 는 본문도 신구법도 다시 받지 않는다.

    결과가 같은 것으로는 부족하다 — 다시 받아서 같은 답을 낸 경우와 구별되지 않는다.
    호출 수를 센다.
    """
    first = await _run(owner_conn, api, store, run_seed="dddd")
    await record_run(owner_conn, first)
    before = (api.count("oldAndNew"), api.count("law"))

    second = await _run(owner_conn, api, store, run_seed="eeee")

    assert second.status is OpsRunStatus.SUCCEEDED_ZERO
    assert second.outcomes[0].status is LawOutcomeStatus.SKIPPED_DONE
    assert second.outcomes[0].change_set_id == first.outcomes[0].change_set_id
    assert (api.count("oldAndNew"), api.count("law")) == before

    async with owner_conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM change_set")
        row = await cur.fetchone()
    assert row is not None and row[0] == 1


# ---------------------------------------------------------------------------
# 실패 격리
# ---------------------------------------------------------------------------


async def test_a_failing_law_does_not_fail_the_whole_run(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """**하루치가 통째로 날아가지 않는다.** 사유는 값으로 남고 상태는 부분 실패다."""
    api.old_and_new_ok = False

    result = await _run(owner_conn, api, store, run_seed="ffff")

    assert result.status is OpsRunStatus.PARTIAL
    outcome = result.outcomes[0]
    assert outcome.status is LawOutcomeStatus.FAILED
    assert outcome.failure_detail is not None

    ops_run_id = await record_run(owner_conn, result)
    async with owner_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT status, failure_detail FROM ops_law_outcome WHERE ops_run_id = %s",
            (ops_run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["status"] == LawOutcomeStatus.FAILED.value
    assert row["failure_detail"]


async def test_a_failed_law_is_retried_on_the_next_run(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """실패는 종결이 아니다. 다음 실행이 다시 시도해 회수한다."""
    api.old_and_new_ok = False
    failed = await _run(owner_conn, api, store, run_seed="0001")
    await record_run(owner_conn, failed)

    api.old_and_new_ok = True
    recovered = await _run(owner_conn, api, store, run_seed="0002")

    assert recovered.status is OpsRunStatus.SUCCEEDED
    assert recovered.outcomes[0].status is LawOutcomeStatus.DIFFED


async def test_canary_failure_stops_before_polling(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """**0으로 기록하는 것보다 '수집 안 함'이 안전하다** (ADR-005).

    폴링을 아예 하지 않았다는 것이 핵심이다 — 폴링하고 0건으로 남기면 그 0이
    "개정 없음"으로 하류에 흘러간다.
    """
    api.canary_ok = False

    result = await _run(owner_conn, api, store, run_seed="0003")

    assert result.status is OpsRunStatus.SKIPPED_CANARY_FAILED
    assert result.probes == ()
    assert result.outcomes == ()
    assert [key for target, key in api.calls if target == "lsJoHstInf"] == [CANARY_DATE]


async def test_a_date_outside_the_corpus_is_zero_not_failure(
    owner_conn: psycopg.AsyncConnection[Any], api: Api, store: LocalDocumentStore
) -> None:
    """코퍼스 대상이 없는 날은 0건 성공이다. 12개월 실측에서 이것이 기본 상태다."""
    result = await _run(owner_conn, api, store, run_seed="0004", dates=("20260113",))

    assert result.status is OpsRunStatus.SUCCEEDED_ZERO
    assert result.outcomes == ()
