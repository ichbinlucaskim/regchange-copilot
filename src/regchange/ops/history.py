"""실행 이력 조회 — `ops history` / `ops summary` / `ops alerts`.

목적:
    "언제부터 돌았고, 며칠 실패했고, 지금 봐야 할 것이 있는가"에 답한다.

구현 이유:
    **로그만으로는 운영 실적이 되지 않는다.** 로그는 grep 해야 하고, grep 한 결과는
    사람마다 다르며, 3개월 뒤에는 회전되어 사라진다. 실행 이력이 테이블에 있고
    질의가 코드에 있으면 같은 질문에 항상 같은 답이 나온다 — 그 재현성이 곧
    "N일간 운영, M건 포착, 실패 K일"이라는 주장의 근거다.

    **알림을 여기 두는 이유**는 R-21의 잔여 리스크가 "기록만 하고 아무도 보지
    않는다"였기 때문이다. 탐지 3(MISMATCH)과 4(change_ratio_exceeded)는 실패를
    만들지 않고 컬럼에만 남는다. 조회 명령이 그 값을 한 화면에 모으는 것이
    지금 단계에서 할 수 있는 최소한이며, 배포 후 CloudWatch 알람으로 승격한다.

    **KST 달력으로 센다.** 기록은 UTC 이지만 "며칠 돌았나"를 UTC 로 세면 07:00 KST
    실행이 전날로 밀린다. 변환은 표시 시점에 한다 (`OPS_CALENDAR_OFFSET`).

트레이드오프:
    출력이 사람이 읽는 텍스트다. JSON 을 내면 기계가 읽기 좋지만, 이 명령의
    1차 소비자는 운영자 자신이고 2차 소비자는 README 다. 기계 소비가 필요해지면
    질의 함수(`fetch_*`)가 이미 값을 돌려주므로 렌더러만 하나 더 붙인다.

    질의를 뷰가 아니라 파이썬에 둔다. 뷰로 만들면 마이그레이션이 늘고, 집계 규칙이
    바뀔 때 과거 행의 해석까지 바뀐다 — 집계는 해석이므로 코드에 두고 버전 관리한다.

엣지 케이스:
    - 실행 이력이 하나도 없음: `fetch_summary`가 None 을 돌려준다. 0일 운영이라는
      값을 지어내지 않는다.
    - 하루에 두 번 실행: 그 날은 실행일 1일로 세되 행은 둘 다 보인다. 수동 실행과
      cron 이 겹치는 정상 경우다.
    - 연속 0건: 알람 임계 전까지는 알람이 아니다. 코퍼스 12개월 실측이 개정일
      14일/365일이므로 **0건이 기본 상태**다 (`CONSECUTIVE_ZERO_ALERT_DAYS`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.rows import dict_row

from regchange.ops.models import OPS_CALENDAR_OFFSET, OpsRunStatus
from regchange.review.queue import count_overdue


class OpsQueryError(RuntimeError):
    """조회가 기대한 형태의 결과를 내지 않았다. 조용한 기본값으로 덮지 않는다."""


DEFAULT_HISTORY_DAYS = 30
"""`ops history`의 기본 조회 구간. 한 달이면 주간 리듬과 월 경계가 함께 보인다."""

DEFAULT_ALERT_DAYS = 7
"""`ops alerts`의 기본 조회 구간. 한 주는 사람이 "최근"이라 부르는 단위다."""

CONSECUTIVE_ZERO_ALERT_DAYS = 60
"""연속 0건이 며칠 이어지면 알람인가 — ADR-005 가 남긴 TODO 의 값.

**실측으로 정했다.** 12개월 전수 캐시(`data/frequency-cache`, 2025-08-01~2026-07-31)에서
현재 코퍼스(활성 법령 9건)의 개정일은 14일이고, 개정일 사이 간격은 다음과 같다:

    7, 7, 8, 14, 14, 16, 19, 21, 28, 28, 34, 42, 49  (중앙값 19일, 최대 49일)

여기에 측정 구간 시작(2025-08-01)부터 첫 개정일(2025-09-23)까지 **53일**이 있는데,
이 구간은 좌측이 잘려 있으므로 실제 무개정 구간은 그보다 길었을 수 있다.

그래서 **60일**로 잡는다. 관측된 최장 구간(53일)보다 크고, 그 위의 첫 라운드 값이다.

- **더 짧게 잡으면** 정상적인 무개정 구간이 알람이 된다. 반복되는 오탐은 알람을
  무시하게 만들고, 무시되는 알람은 없는 것과 같다.
- **더 길게 잡으면** 탐지 실패가 두 달 넘게 조용하다. 다만 계통적 실패는 **매 실행
  카나리아**가 먼저 잡는다 — 이 알람이 담당하는 것은 카나리아가 통과하는데도
  우리 대상만 계속 0건인 경우(요청 파라미터·코퍼스 설정 오류)다.

**세는 단위는 달력일이 아니라 실행이다.** 노트북이 꺼져 있던 날은 0건 관측이
아니므로 세지 않는다. 대신 연속 0건 실행이 걸쳐 있는 **달력 구간**을 함께 보고해,
"3번 실행했는데 그 사이가 70일"인 상태가 숨지 않게 한다.

이 값은 관측 1년치에 근거한다. 운영 데이터가 쌓이면 재산정한다.
"""


class AlertKind(StrEnum):
    """알림 종류. 값이 곧 운영자가 봐야 할 이유다."""

    CANARY_FAILED = "CANARY_FAILED"
    """카나리아 실패로 수집하지 않은 실행이 있다."""

    RUN_FAILED = "RUN_FAILED"
    """실행이 실패했거나 일부만 처리됐다."""

    MST_MISMATCH = "MST_MISMATCH"
    """자동 확보한 직전 MST 와 실제 비교에 쓴 MST 가 다르다 (R-21 탐지 3)."""

    CHANGE_RATIO_EXCEEDED = "CHANGE_RATIO_EXCEEDED"
    """변경 조문 비율이 임계를 넘었다 (R-21 탐지 4). 전부개정이 아니면 짝을 의심한다."""

    CONSECUTIVE_ZERO = "CONSECUTIVE_ZERO"
    """연속 0건이 임계를 넘었다. 코퍼스 설정이나 요청 파라미터를 의심한다."""

    REVIEW_OVERDUE = "REVIEW_OVERDUE"
    """검토 대기 건이 기한(개정 시행일)을 넘겼다. **담당자를 재촉할 사실이다.**"""

    REVIEW_DUE_UNKNOWN = "REVIEW_DUE_UNKNOWN"
    """검토 대기 건의 기한을 모른다 — 개정 조문의 시행일을 확보하지 못했다.

    `REVIEW_OVERDUE` 와 **합치지 않는다.** 전자는 담당자를 재촉할 사실이고 이것은
    수집 경로를 고칠 사실이다. 조치가 다르면 지표도 달라야 한다. 임의의 기본 기한으로
    채우면 이 구별이 사라지고 근거 없는 숫자가 운영 지표가 된다 (마이그레이션 011 §5)."""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """실행 하나의 이력 행. 실패 사유를 함께 담는다."""

    run_date: dt.date
    """KST 달력의 실행일. 저장은 UTC 이며 이 값은 표시용 변환 결과다."""

    run_id: str
    status: OpsRunStatus
    laws_detected: int
    laws_diffed: int
    laws_failed: int
    change_sets_created: int
    articles_changed: int
    retries: int
    detail: str
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpsSummary:
    """운영 시작부터 오늘까지의 집계. README 와 이력서에 그대로 들어간다."""

    first_run_date: dt.date
    last_run_date: dt.date
    calendar_days: int
    """첫 실행일부터 오늘까지의 달력 일수(양 끝 포함)."""

    executed_days: int
    succeeded_days: int
    zero_days: int
    partial_days: int
    failed_days: int
    canary_skipped_days: int
    total_runs: int
    laws_diffed: int
    change_sets_created: int
    articles_changed: int

    @property
    def missing_days(self) -> int:
        """실행 기록이 없는 날. 노트북이 꺼져 있었거나 프로세스가 죽은 날이다."""
        return self.calendar_days - self.executed_days


@dataclass(frozen=True, slots=True)
class Alert:
    """알림 한 건. 언제·무엇을·어디서 봐야 하는지를 담는다."""

    kind: AlertKind
    occurred_at: dt.datetime
    subject: str
    detail: str


def _since(days: int, now: dt.datetime) -> dt.datetime:
    """조회 시작 시각. 달력일 기준으로 자른다."""
    return now - dt.timedelta(days=days)


async def fetch_history(
    conn: psycopg.AsyncConnection[Any], *, days: int, now: dt.datetime
) -> tuple[HistoryRow, ...]:
    """최근 N일의 실행 이력을 최신순으로 돌려준다.

    목적:
        "언제 돌았고 그날 무엇이 잡혔고 무엇이 실패했나"를 한 번에 본다.

    구현 이유:
        실패 사유를 같은 행에 붙인다. 별도 명령으로 나누면 실패를 본 사람이 사유를
        보러 한 번 더 움직여야 하고, 한 번 더 움직여야 하는 정보는 보지 않게 된다.

    트레이드오프:
        실패 사유를 실행마다 별도 질의로 가져온다(N+1). 실행 수가 30건 규모이므로
        비용이 없고, 한 질의로 합치면 실행 행이 실패 수만큼 중복돼 집계가 어긋난다.

    엣지 케이스:
        - 이력이 없음: 빈 튜플. 예외가 아니다.
        - `days`가 0 이하: `ValueError`. 빈 구간은 "실행 없음"으로 위장한다.
    """
    if days < 1:
        raise ValueError(f"조회 일수는 1 이상이어야 한다 (받은 값 {days})")

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, run_id, status, laws_detected, laws_diffed, laws_failed,
                   change_sets_created, articles_changed, retries, detail,
                   (started_at AT TIME ZONE 'UTC' + %(offset)s)::date AS run_date
              FROM ops_run
             WHERE started_at >= %(since)s
             ORDER BY started_at DESC
            """,
            {"since": _since(days, now), "offset": OPS_CALENDAR_OFFSET},
        )
        runs = await cur.fetchall()

        rows: list[HistoryRow] = []
        for run in runs:
            await cur.execute(
                """
                SELECT law_id, law_name, mst, failure_detail
                  FROM ops_law_outcome
                 WHERE ops_run_id = %s AND status = 'FAILED'
                 ORDER BY reg_date, law_id
                """,
                (run["id"],),
            )
            failures = tuple(
                f"{row['law_name'] or row['law_id']} (MST {row['mst']}): {row['failure_detail']}"
                for row in await cur.fetchall()
            )
            rows.append(
                HistoryRow(
                    run_date=run["run_date"],
                    run_id=run["run_id"],
                    status=OpsRunStatus(run["status"]),
                    laws_detected=run["laws_detected"],
                    laws_diffed=run["laws_diffed"],
                    laws_failed=run["laws_failed"],
                    change_sets_created=run["change_sets_created"],
                    articles_changed=run["articles_changed"],
                    retries=run["retries"],
                    detail=run["detail"],
                    failures=failures,
                )
            )
    return tuple(rows)


async def fetch_summary(
    conn: psycopg.AsyncConnection[Any], *, now: dt.datetime
) -> OpsSummary | None:
    """운영 시작일부터 오늘까지를 집계한다. 이력이 없으면 None.

    목적:
        "N일간 운영, M건 포착, 실패 K일"을 그대로 출력한다.

    구현 이유:
        **일 단위로 접어서 센다.** 하루에 두 번 실행한 날(수동 + cron)을 이틀로
        세면 운영 일수가 부풀려진다. 접을 때는 그날의 **가장 나쁜 상태**를 그 날의
        상태로 삼는다 — 성공한 실행이 실패한 실행을 덮으면 실패가 사라진다.

        미실행 일수를 빼서 구하지 않고 달력에서 실행일을 빼서 구한다. 그래야
        "돌지 않은 날"이 기록의 부재로부터 계산되며, 부재는 기록할 수 없다.

    트레이드오프:
        "가장 나쁜 상태"의 순서를 코드가 정한다(FAILED > PARTIAL >
        SKIPPED_CANARY_FAILED > SUCCEEDED_ZERO > SUCCEEDED). 카나리아 실패를
        실패보다 아래에 둔 이유는 그것이 미수행이기 때문이다.

    엣지 케이스:
        - 실행이 하나도 없음: None. 0일 운영이라는 값을 만들지 않는다.
        - 첫 실행이 오늘: `calendar_days=1`, `missing_days=0`.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT (started_at AT TIME ZONE 'UTC' + %(offset)s)::date AS run_date, status
              FROM ops_run
             ORDER BY started_at
            """,
            {"offset": OPS_CALENDAR_OFFSET},
        )
        runs = await cur.fetchall()
        if not runs:
            return None

        await cur.execute(
            """
            SELECT count(*)                          AS total_runs,
                   coalesce(sum(laws_diffed), 0)     AS laws_diffed,
                   coalesce(sum(change_sets_created), 0) AS change_sets_created,
                   coalesce(sum(articles_changed), 0)    AS articles_changed
              FROM ops_run
            """
        )
        totals = await cur.fetchone()
    if totals is None:
        raise OpsQueryError("집계 질의가 행을 돌려주지 않았다. count(*) 는 항상 한 행이어야 한다")

    worst: dict[dt.date, OpsRunStatus] = {}
    for run in runs:
        day = run["run_date"]
        status = OpsRunStatus(run["status"])
        current = worst.get(day)
        if current is None or _severity(status) > _severity(current):
            worst[day] = status

    first = min(worst)
    today = (now.astimezone(dt.UTC) + OPS_CALENDAR_OFFSET).date()
    return OpsSummary(
        first_run_date=first,
        last_run_date=max(worst),
        calendar_days=(today - first).days + 1,
        executed_days=len(worst),
        succeeded_days=sum(1 for s in worst.values() if s is OpsRunStatus.SUCCEEDED),
        zero_days=sum(1 for s in worst.values() if s is OpsRunStatus.SUCCEEDED_ZERO),
        partial_days=sum(1 for s in worst.values() if s is OpsRunStatus.PARTIAL),
        failed_days=sum(1 for s in worst.values() if s is OpsRunStatus.FAILED),
        canary_skipped_days=sum(
            1 for s in worst.values() if s is OpsRunStatus.SKIPPED_CANARY_FAILED
        ),
        total_runs=totals["total_runs"],
        laws_diffed=totals["laws_diffed"],
        change_sets_created=totals["change_sets_created"],
        articles_changed=totals["articles_changed"],
    )


def _severity(status: OpsRunStatus) -> int:
    """하루에 여러 실행이 있을 때 어느 상태를 그 날의 상태로 삼는가.

    성공이 실패를 덮지 않게 하는 것이 목적이다. 카나리아 실패는 미수행이므로
    실패·부분실패보다 아래에 둔다.
    """
    order = {
        OpsRunStatus.SUCCEEDED: 0,
        OpsRunStatus.SUCCEEDED_ZERO: 1,
        OpsRunStatus.SKIPPED_CANARY_FAILED: 2,
        OpsRunStatus.PARTIAL: 3,
        OpsRunStatus.FAILED: 4,
    }
    return order[status]


async def consecutive_zero(
    conn: psycopg.AsyncConnection[Any], *, now: dt.datetime
) -> tuple[int, int]:
    """(연속 0건 실행 수, 그 구간의 달력 일수)를 돌려준다.

    가장 최근 실행부터 거슬러 올라가며 `SUCCEEDED_ZERO`가 이어지는 동안 센다.
    달력 일수를 함께 돌려주는 이유는 실행이 드문드문한 경우 "3회 연속"이 실제로는
    두 달일 수 있기 때문이다 — 실행 수만 보면 그 사실이 숨는다.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT status, started_at
              FROM ops_run
             ORDER BY started_at DESC
            """
        )
        runs = await cur.fetchall()

    streak = 0
    oldest: dt.datetime | None = None
    for run in runs:
        if OpsRunStatus(run["status"]) is not OpsRunStatus.SUCCEEDED_ZERO:
            break
        streak += 1
        oldest = run["started_at"]
    if oldest is None:
        return 0, 0
    return streak, (now - oldest).days


async def fetch_alerts(
    conn: psycopg.AsyncConnection[Any], *, days: int, now: dt.datetime
) -> tuple[Alert, ...]:
    """최근 N일의 알림을 모은다. 없으면 빈 튜플.

    목적:
        기록만 되고 아무도 보지 않던 값을 한 화면에 모은다 (R-21 잔여 리스크).

    구현 이유:
        MISMATCH 와 change_ratio_exceeded 는 `ops_law_outcome`이 아니라
        `change_set`에서 읽는다. **자동 실행 밖에서 만들어진 비교도 보여야 하기
        때문이다** — `regchange diff auto`로 손수 돌린 것도 같은 위험을 갖는다.

    트레이드오프:
        알림을 보낼 곳이 없다. 지금은 조회 명령이 전부이며, 그것이 이 단계의
        결정이다(발송은 승인 절차와 dispatch 경계의 일이다). 배포 후 CloudWatch
        알람으로 승격한다.

    엣지 케이스:
        - 연속 0건이 임계 미만: 알림을 만들지 않는다. 0건은 기본 상태다.
        - 같은 실행이 여러 알림을 만들 수 있다(카나리아 실패 + 연속 0건). 중복
          제거하지 않는다 — 서로 다른 이유이며 조치도 다르다.
    """
    if days < 1:
        raise ValueError(f"조회 일수는 1 이상이어야 한다 (받은 값 {days})")

    since = _since(days, now)
    alerts: list[Alert] = []

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT run_id, status, started_at, detail
              FROM ops_run
             WHERE started_at >= %(since)s
               AND status IN ('SKIPPED_CANARY_FAILED', 'FAILED', 'PARTIAL')
             ORDER BY started_at DESC
            """,
            {"since": since},
        )
        for row in await cur.fetchall():
            status = OpsRunStatus(row["status"])
            kind = (
                AlertKind.CANARY_FAILED
                if status is OpsRunStatus.SKIPPED_CANARY_FAILED
                else AlertKind.RUN_FAILED
            )
            alerts.append(
                Alert(
                    kind=kind,
                    occurred_at=row["started_at"],
                    subject=f"{row['run_id']} ({status.value})",
                    detail=row["detail"],
                )
            )

        await cur.execute(
            """
            SELECT cs.law_id, cs.computed_at, cs.resolved_from_mst,
                   cs.change_ratio_exceeded, cs.mst_resolution_source,
                   cs.revision_kind, f.mst AS from_mst, t.mst AS to_mst
              FROM change_set cs
              JOIN regulation_document f ON f.id = cs.from_document_id
              JOIN regulation_document t ON t.id = cs.to_document_id
             WHERE cs.computed_at >= %(since)s
               AND (cs.mst_resolution_source = 'MISMATCH' OR cs.change_ratio_exceeded)
             ORDER BY cs.computed_at DESC
            """,
            {"since": since},
        )
        for row in await cur.fetchall():
            subject = f"법령ID {row['law_id']} {row['from_mst']} → {row['to_mst']}"
            if row["mst_resolution_source"] == "MISMATCH":
                alerts.append(
                    Alert(
                        kind=AlertKind.MST_MISMATCH,
                        occurred_at=row["computed_at"],
                        subject=subject,
                        detail=(
                            f"자동 확보값 {row['resolved_from_mst']} 와 실제 사용값 "
                            f"{row['from_mst']} 가 다르다. 수동 지정이면 정상이다"
                        ),
                    )
                )
            if row["change_ratio_exceeded"]:
                alerts.append(
                    Alert(
                        kind=AlertKind.CHANGE_RATIO_EXCEEDED,
                        occurred_at=row["computed_at"],
                        subject=subject,
                        detail=(
                            f"변경 조문 비율이 임계를 넘었다 (제개정구분 "
                            f"{row['revision_kind']}). 전부개정이 아니면 다른 법령을 "
                            "비교했을 가능성을 먼저 의심한다"
                        ),
                    )
                )

    # 검토 큐 — 4단계에서 붙었다. 알림을 보낼 곳은 여전히 없고 조회 명령이 전부다.
    review = await count_overdue(conn, now=now)
    if review.overdue:
        alerts.append(
            Alert(
                kind=AlertKind.REVIEW_OVERDUE,
                occurred_at=now,
                subject=f"검토 기한 초과 {review.overdue}건 (대기 {review.pending}건)",
                detail=(
                    "기한은 개정 조문의 시행일이다. 시행일까지 사내 규정이 정비되어 "
                    f"있어야 하며, 가장 오래 기다린 건이 {review.oldest_pending_days}일째다"
                ),
            )
        )
    if review.unknown_due:
        alerts.append(
            Alert(
                kind=AlertKind.REVIEW_DUE_UNKNOWN,
                occurred_at=now,
                subject=f"기한을 모르는 검토 대기 {review.unknown_due}건",
                detail=(
                    "개정 조문의 시행일을 확보하지 못한 건이다. 기한 초과로 세지 않는다 — "
                    "재촉할 사실이 아니라 수집 경로를 고칠 사실이다"
                ),
            )
        )

    streak, span = await consecutive_zero(conn, now=now)
    if span >= CONSECUTIVE_ZERO_ALERT_DAYS:
        alerts.append(
            Alert(
                kind=AlertKind.CONSECUTIVE_ZERO,
                occurred_at=now,
                subject=f"연속 0건 실행 {streak}회 / {span}일",
                detail=(
                    f"임계 {CONSECUTIVE_ZERO_ALERT_DAYS}일을 넘었다. 코퍼스 설정과 "
                    "요청 파라미터를 확인한다 — 카나리아는 통과하는데 우리 대상만 "
                    "계속 0건인 상태다"
                ),
            )
        )
    return tuple(alerts)
