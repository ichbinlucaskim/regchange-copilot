"""조회 결과를 사람이 읽는 줄로 만든다. I/O 도 질의도 하지 않는다.

목적:
    `ops daily` / `history` / `summary` / `alerts` 의 표준출력 형식을 한 곳에 모은다.

구현 이유:
    출력 형식을 CLI 안에 두면 형식을 검증하려고 프로세스를 띄우거나 stdout 을
    가로채야 한다. 값 → 줄 목록으로 분리하면 그냥 함수 테스트다. **이 출력이
    README 와 이력서에 그대로 들어가므로** 형식이 조용히 바뀌는 것도 막아야 한다.

트레이드오프:
    고정폭 정렬을 문자열 포맷으로 손수 맞춘다. 테이블 라이브러리를 쓰면 예뻐지지만
    의존성이 늘고, 이 출력은 열이 6개를 넘지 않는다.

    한국어 문자열의 표시 폭(2칸)을 계산하지 않는다. 법령명이 섞인 열은 정렬이
    어긋나 보일 수 있다. 폭 계산을 넣으면 `unicodedata.east_asian_width` 로
    한 겹이 더 생기는데, 정렬이 어긋나는 열은 마지막 열 하나뿐이라 실익이 없다.

엣지 케이스:
    - 이력 0건: "기록 없음"을 명시한다. 빈 출력은 "명령이 실패했나"로 읽힌다.
    - 실패 사유가 여러 줄: 실행 행 아래에 들여쓰기로 붙인다. 잘라내지 않는다 —
      사유의 뒷부분에 원인이 있는 경우가 많다.
"""

from __future__ import annotations

from regchange.ops.history import Alert, HistoryRow, OpsSummary
from regchange.ops.models import DailyRunResult, LawOutcomeStatus, OpsRunStatus

_STATUS_MARK = {
    OpsRunStatus.SUCCEEDED: "OK",
    OpsRunStatus.SUCCEEDED_ZERO: "0건",
    OpsRunStatus.PARTIAL: "부분",
    OpsRunStatus.FAILED: "실패",
    OpsRunStatus.SKIPPED_CANARY_FAILED: "미수행",
}
"""상태의 짧은 표기. 값 자체(`SUCCEEDED`)는 길어서 표가 깨진다."""


def render_daily(result: DailyRunResult) -> list[str]:
    """일일 실행 결과를 줄 목록으로 만든다. 실패를 먼저 보여준다."""
    lines = [
        f"run_id      {result.run_id}",
        f"상태        {result.status.value}",
        f"카나리아    {'통과' if result.canary.passed else '실패'} "
        f"(totalCnt={result.canary.total_count})",
        f"폴링 대상   {result.lookback_days}일 {list(result.target_dates)}",
    ]
    for probe in result.probes:
        mark = "실패" if probe.failed else "정상"
        lines.append(
            f"  {probe.reg_date}  {mark}  전체 {probe.total_count} / 코퍼스 {probe.matched}"
        )

    lines.append(f"요약        {result.detail}")
    for outcome in result.outcomes:
        name = outcome.detected.law_name or outcome.detected.law_id
        head = f"  [{outcome.status.value}] {name} (MST {outcome.detected.mst})"
        if outcome.status is LawOutcomeStatus.DIFFED:
            head += (
                f" ← {outcome.from_mst} · 변경 {outcome.articles_changed}조문"
                f" · {outcome.mst_resolution_source}"
            )
            if outcome.change_ratio_exceeded:
                head += " · 변경규모 임계 초과"
        lines.append(head)
        if outcome.failure_detail is not None:
            lines.append(f"      사유: {outcome.failure_detail}")
    return lines


def render_history(rows: tuple[HistoryRow, ...], *, days: int) -> list[str]:
    """실행 이력을 최신순 표로 만든다."""
    lines = [f"최근 {days}일 실행 이력 ({len(rows)}건)", ""]
    if not rows:
        lines.append("  기록 없음. 아직 실행하지 않았거나 조회 구간 밖이다")
        return lines

    lines.append("  날짜         상태    대상  diff  실패  변경조문  재시도  run_id")
    for row in rows:
        lines.append(
            f"  {row.run_date}  {_STATUS_MARK[row.status]:<6}"
            f"{row.laws_detected:>4}{row.laws_diffed:>6}{row.laws_failed:>6}"
            f"{row.articles_changed:>10}{row.retries:>8}  {row.run_id}"
        )
        lines.extend(f"      실패: {failure}" for failure in row.failures)
    return lines


def render_summary(summary: OpsSummary | None) -> list[str]:
    """운영 집계를 줄 목록으로 만든다. 실패를 숨기지 않는다."""
    if summary is None:
        return ["운영 기록이 없다. `regchange ops daily` 를 한 번도 실행하지 않았다"]

    return [
        f"운영 기간    {summary.first_run_date} ~ {summary.last_run_date} "
        f"({summary.calendar_days}일)",
        f"실행일       {summary.executed_days}일 (실행 {summary.total_runs}회)",
        f"  성공       {summary.succeeded_days}일",
        f"  0건 성공   {summary.zero_days}일",
        f"  부분 실패  {summary.partial_days}일",
        f"  실패       {summary.failed_days}일",
        f"  미수행     {summary.canary_skipped_days}일 (카나리아 실패)",
        f"미실행일     {summary.missing_days}일",
        "",
        f"포착         법령 {summary.laws_diffed}건 / change_set "
        f"{summary.change_sets_created}건 / 변경 {summary.articles_changed}조문",
    ]


def render_alerts(alerts: tuple[Alert, ...], *, days: int) -> list[str]:
    """알림을 종류별로 묶어 보여준다."""
    lines = [f"최근 {days}일 알림 ({len(alerts)}건)", ""]
    if not alerts:
        lines.append("  알림 없음")
        return lines

    for alert in alerts:
        lines.append(f"  [{alert.kind.value}] {alert.occurred_at:%Y-%m-%d %H:%MZ} {alert.subject}")
        lines.append(f"      {alert.detail}")
    return lines
