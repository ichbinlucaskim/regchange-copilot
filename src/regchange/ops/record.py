"""실행 결과를 `ops_run` / `ops_law_outcome` 에 남긴다.

목적:
    하루 실행 하나를 **성공이든 실패든** 행으로 남겨, "언제부터 돌았고 며칠
    실패했나"에 답할 수 있게 한다.

구현 이유:
    기록을 실행 로직(`ops.daily`)에서 떼어냈다. 두 가지가 다른 이유로 실패하기
    때문이다 — 실행은 네트워크와 외부 API 때문에, 기록은 DB와 제약 때문에
    실패한다. 한 함수에 있으면 "수집은 됐는데 기록이 안 됐다"를 구별할 수 없다.

    **행을 실행이 끝난 뒤 한 번에 쓴다.** `load_run`과 같은 발상이지만 결론이
    다르다 — `load_run`은 행의 존재가 곧 완료이므로 실패하면 행이 없다. 여기서는
    실패한 실행도 행을 남겨야 하므로, 호출부가 예외를 잡아 `record_failure`로
    실패 행을 쓴다. **행이 없는 날은 "실패한 날"이 아니라 "실행하지 않은 날"**이며
    그 구별이 이 테이블의 존재 이유다.

트레이드오프:
    프로세스가 SIGKILL 로 죽으면 아무 행도 남지 않아 "미실행"으로 보인다. 시작
    시점에 행을 쓰고 끝에 갱신하면 그 창이 닫히지만, 그러려면 UPDATE 를 허용해야
    한다 — 운영 실적을 사후에 고칠 수 있게 만드는 대가가 더 크다. 대신 래퍼
    스크립트가 표준출력 로그를 남기므로 그 창의 흔적은 파일에 있다.

엣지 케이스:
    - 같은 `run_id`로 두 번 기록: 유니크 인덱스가 거부한다. `run_id`는 실행마다
      새로 만들어지므로 정상 경로에서 발생하지 않는다.
    - `laws_*` 합이 `laws_detected`와 다름: CHECK 제약이 거부한다. 처분을 세지
      않은 경로가 생기면 여기서 드러난다 (`LoadCounts.verify_partition`과 같은 발상).
    - 실행 결과에 법령이 0건: `ops_run`만 쓰고 `ops_law_outcome`은 쓰지 않는다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog
from psycopg.types.json import Json

from regchange.ingest.snapshot import utc_now
from regchange.ops.models import (
    DailyRunResult,
    DateProbe,
    LawOutcome,
    LawOutcomeStatus,
    OpsRunStatus,
)

_log = structlog.get_logger(__name__)

_INSERT_RUN = """
INSERT INTO ops_run (
    id, run_id, started_at, finished_at, status,
    lookback_days, target_dates, canary_passed, canary_total_count, date_probes,
    laws_detected, laws_diffed, laws_skipped, laws_no_previous, laws_failed,
    change_sets_created, articles_changed, requests, retries, detail
) VALUES (
    %(id)s, %(run_id)s, %(started_at)s, %(finished_at)s, %(status)s,
    %(lookback_days)s, %(target_dates)s, %(canary_passed)s, %(canary_total_count)s,
    %(date_probes)s,
    %(laws_detected)s, %(laws_diffed)s, %(laws_skipped)s, %(laws_no_previous)s,
    %(laws_failed)s,
    %(change_sets_created)s, %(articles_changed)s, %(requests)s, %(retries)s, %(detail)s
)
"""

_INSERT_OUTCOME = """
INSERT INTO ops_law_outcome (
    id, ops_run_id, reg_date, law_id, law_name, mst, status,
    change_set_id, from_mst, mst_resolution_source, change_ratio_exceeded,
    articles_changed, failure_detail
) VALUES (
    %(id)s, %(ops_run_id)s, %(reg_date)s, %(law_id)s, %(law_name)s, %(mst)s, %(status)s,
    %(change_set_id)s, %(from_mst)s, %(mst_resolution_source)s, %(change_ratio_exceeded)s,
    %(articles_changed)s, %(failure_detail)s
)
"""


def probe_payload(probes: tuple[DateProbe, ...]) -> list[dict[str, object]]:
    """날짜별 폴링 결과를 jsonb 로 직렬화한다.

    `status`를 문자열로 풀어 둔다 — enum 값이 바뀌면 과거 행의 해석이 달라지지만,
    저장된 것은 그때의 문자열이므로 그 시점의 판정이 보존된다 (원칙 6과 같은 방향).
    """
    return [
        {
            "reg_date": probe.reg_date,
            "status": probe.status.value,
            "total_count": probe.total_count,
            "matched": probe.matched,
            "detail": probe.detail,
        }
        for probe in probes
    ]


async def record_run(conn: psycopg.AsyncConnection[Any], result: DailyRunResult) -> UUID:
    """실행 결과 한 건을 기록하고 `ops_run.id` 를 돌려준다.

    목적:
        실행 이력과 법령별 처분을 같은 트랜잭션에 남긴다.

    구현 이유:
        한 트랜잭션으로 묶는다. 실행 행만 있고 법령 행이 없는 상태는 "대상이
        0건이었다"와 구별되지 않는다 — 그 구별 불가가 이 저장소가 반복해서 겪은
        실패 형태다.

    트레이드오프:
        법령이 많은 날은 INSERT 가 그만큼 늘어난다. `executemany`로 묶지 않은
        이유는 하루 대상이 최대 수 건이기 때문이다(코퍼스 9개 법령, 12개월 개정일
        14일). 배치 최적화가 필요해지면 코퍼스가 커진 것이고, 그때 다시 판단한다.

    엣지 케이스:
        - `SKIPPED_CANARY_FAILED`: 법령 행이 0건이고 `laws_detected`도 0이다.
          "대상이 없었다"가 아니라 "보지 않았다"이며, 상태 값이 그것을 말한다.
    """
    run_uuid = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            _INSERT_RUN,
            {
                "id": run_uuid,
                "run_id": result.run_id,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "status": result.status.value,
                "lookback_days": max(result.lookback_days, 1),
                "target_dates": list(result.target_dates),
                "canary_passed": result.canary.passed,
                "canary_total_count": result.canary.total_count,
                "date_probes": Json(probe_payload(result.probes)),
                "laws_detected": len(result.outcomes),
                "laws_diffed": result.count(LawOutcomeStatus.DIFFED),
                "laws_skipped": result.count(LawOutcomeStatus.SKIPPED_DONE),
                "laws_no_previous": result.count(LawOutcomeStatus.NO_PREVIOUS),
                "laws_failed": result.count(LawOutcomeStatus.FAILED),
                "change_sets_created": result.change_sets_created,
                "articles_changed": result.articles_changed,
                "requests": result.requests,
                "retries": result.retries,
                "detail": result.detail,
            },
        )
        for outcome in result.outcomes:
            await cur.execute(_INSERT_OUTCOME, _outcome_params(run_uuid, outcome))
    await conn.commit()

    _log.info(
        "ops.run_recorded",
        ops_run_id=str(run_uuid),
        run_id=result.run_id,
        status=result.status.value,
        laws=len(result.outcomes),
        change_sets=result.change_sets_created,
    )
    return run_uuid


def _outcome_params(run_uuid: UUID, outcome: LawOutcome) -> dict[str, object]:
    """법령 처분 한 건의 바인딩 값."""
    return {
        "id": uuid4(),
        "ops_run_id": run_uuid,
        "reg_date": outcome.detected.reg_date,
        "law_id": outcome.detected.law_id,
        "law_name": outcome.detected.law_name,
        "mst": outcome.detected.mst,
        "status": outcome.status.value,
        "change_set_id": outcome.change_set_id,
        "from_mst": outcome.from_mst,
        "mst_resolution_source": outcome.mst_resolution_source,
        "change_ratio_exceeded": outcome.change_ratio_exceeded,
        "articles_changed": outcome.articles_changed,
        "failure_detail": outcome.failure_detail,
    }


async def record_failure(
    conn: psycopg.AsyncConnection[Any],
    *,
    run_id: str,
    started_at: dt.datetime,
    dates: tuple[str, ...],
    detail: str,
) -> UUID:
    """실행이 예외로 끝났을 때 실패 행 하나를 남긴다.

    목적:
        기반 실패(저장소·DB·설정)로 죽은 실행이 **미실행으로 보이지 않게** 한다.

    구현 이유:
        `run_daily`는 법령 단위 실패만 값으로 바꾼다. 그 위 층의 실패는 예외로
        올라오며, 잡지 않으면 그날은 행이 없어 "노트북이 꺼져 있었다"와 같은
        모양이 된다. **두 가지는 조치가 다르다** — 하나는 코드를 봐야 하고 하나는
        아무것도 할 것이 없다.

    트레이드오프:
        기록 자체가 실패하면(DB 가 죽은 경우) 남길 방법이 없다. 그 경우 래퍼
        스크립트의 표준출력 로그가 유일한 흔적이다. 파일 로그를 2차 기록으로
        두는 것은 그래서다.

    엣지 케이스:
        - 카나리아조차 못 돌린 경우: `canary_passed=false`, `canary_total_count`는
          NULL 이다. 카나리아 실패(`SKIPPED_CANARY_FAILED`)와 구별하기 위해 상태는
          `FAILED`를 쓴다 — 전자는 "물어봤고 이상했다", 후자는 "묻지도 못했다"이다.
    """
    run_uuid = uuid4()
    async with conn.cursor() as cur:
        await cur.execute(
            _INSERT_RUN,
            {
                "id": run_uuid,
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": utc_now(),
                "status": OpsRunStatus.FAILED.value,
                "lookback_days": max(len(dates), 1),
                "target_dates": list(dates),
                "canary_passed": False,
                "canary_total_count": None,
                "date_probes": Json([]),
                "laws_detected": 0,
                "laws_diffed": 0,
                "laws_skipped": 0,
                "laws_no_previous": 0,
                "laws_failed": 0,
                "change_sets_created": 0,
                "articles_changed": 0,
                "requests": 0,
                "retries": 0,
                "detail": detail,
            },
        )
    await conn.commit()
    _log.error("ops.run_failed", run_id=run_id, detail=detail)
    return run_uuid
