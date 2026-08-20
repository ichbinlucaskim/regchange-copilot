"""일일 작업 — 카나리아 → 폴링 → 코퍼스 필터 → 적재 → diff → 기록.

목적:
    cron 이 한 줄로 부르는 진입점. 하루(정확히는 최근 N일)의 법령 개정을 포착해
    `change_set`까지 만들고, 그 실행 자체를 `ops_run`에 남긴다.

구현 이유:
    **한 법령의 실패가 실행 전체를 실패시키지 않는다.** 이것이 이 모듈의 형태를
    결정한 제약이다. 예외를 그대로 올려 보내면 하루치가 통째로 날아가고, 그날의
    다른 법령 개정은 다음 실행의 재확인 창이 회수해 줄 때까지 보이지 않는다.
    그래서 법령 단위로 `except`하고 사유를 `ops_law_outcome`에 남긴다 —
    **예외를 삼키는 것이 아니라 실패를 값으로 바꿔 기록한다.**

    **멱등성을 DB 조회로 확보한다.** 최근 N일 재확인은 이미 처리한 MST를 매일 다시
    만난다. 그때 `autodiff`를 다시 부르면 (1) `oldAndNew` 호출이 매일 반복되고
    (2) `resolve_previous_mst`가 같은 MST의 스냅샷을 실행마다 새로 쓴다. 그래서
    처리 전에 "이 MST를 이미 처리했는가"를 묻고, 그렇다면 `SKIPPED_DONE`으로 끝낸다.

    **폴링 응답 스냅샷은 새로 처리할 MST가 있는 날짜에만 쓴다.** 매일 7일치를
    저장하면 거의 같은 내용이 7배로 쌓인다(일자별 응답이 하루 180KB~2.7MB이므로
    연 수백 MB다). 그 대신 diff를 만든 날의 근거 응답은 반드시 남는다 —
    "이 change_set을 왜 만들었는가"의 출처이기 때문이다.

트레이드오프:
    위 결정의 대가는 **0건인 날의 응답 원문이 남지 않는다**는 것이다. 사후에
    "그날 정말 0건이었나"를 원문으로 증명할 수 없고, `DateProbe.total_count`와
    `confirm_zero`의 재요청 기록에 의존해야 한다. 0건 판정의 방어를 응답 보관이
    아니라 재요청에 둔 ADR-005의 설계와 같은 방향이므로 이 교환을 택했다.

    또 하나: 실행 전체를 한 커넥션·한 프로세스에서 순차로 돈다. 법령을 병렬로
    처리하면 빨라지지만 공공 API 호출 간격(1.2초)이 클라이언트 인스턴스에 묶여
    있고, 인스턴스를 여러 개 만들면 그 간격이 지켜지지 않는다.

엣지 케이스:
    - 카나리아 실패: 폴링을 아예 하지 않고 `SKIPPED_CANARY_FAILED`로 끝낸다.
      **0건으로 기록하지 않는다** — 그 0은 "개정 없음"으로 하류에 흘러간다.
    - 같은 MST가 창 안의 여러 날짜에 나타남: 한 번만 처리한다. 두 번 처리하면
      `resolve_previous_mst`가 같은 스냅샷 디렉터리를 두 번 쓰려다 실패한다.
    - 제정본: `NO_PREVIOUS`. 실패가 아니며 다음 실행에서 다시 시도하지도 않는다.
    - 법령 처리 중 예외: 트랜잭션을 롤백하고 다음 법령으로 넘어간다. 롤백하지
      않으면 커넥션이 aborted 상태로 남아 **뒤의 모든 법령이 연쇄 실패한다.**
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID
from xml.etree.ElementTree import Element

import psycopg
import structlog

from regchange.adapters.storage.base import DocumentStore
from regchange.config.corpus import CorpusConfig
from regchange.ingest.canary import (
    CanaryResult,
    DailyIngestResult,
    poll_promulgation_date,
    probe_canary,
)
from regchange.ingest.client import PAGE_SIZE, Collection, LawApiClient
from regchange.ingest.masking import Masker
from regchange.ingest.snapshot import utc_now, write_snapshot
from regchange.ops.models import (
    DailyRunResult,
    DateProbe,
    DetectedLaw,
    LawOutcome,
    LawOutcomeStatus,
    derive_status,
)
from regchange.pipeline import AutoDiffOutcome, autodiff

_log = structlog.get_logger(__name__)

LAW_NAME_PATH = "법령정보/법령명한글"
"""일자별 이력 항목에서 법령명의 경로. 근거: `dayjochg_regdt20250114.xml`.

`TargetSpec`에 넣지 않은 이유: 법령명은 **식별자가 아니라 표시용**이다. spec의
경로들은 전부 식별·검증에 쓰이는 값이고, 표시용 필드를 거기 섞으면 "이 경로가
틀리면 무엇이 깨지는가"의 답이 경로마다 달라진다. 여기서는 틀려도 이름이 비는 것뿐이다.
"""


def extract_detected(
    collection: Collection, reg_date: str, law_ids: frozenset[str]
) -> tuple[DetectedLaw, ...]:
    """폴링 결과에서 코퍼스 대상 법령 버전만 골라낸다.

    목적:
        "그날 개정된 전체"에서 "우리가 감시하는 것"으로 좁힌다.

    구현 이유:
        `spec.law_id_path`와 `spec.identity_path`를 쓴다 — 경로를 여기 하드코딩하면
        같은 사실이 두 곳에 생긴다. 그리고 **법령ID가 아니라 MST 단위로 돌려준다.**
        같은 법령ID에 다른 MST는 연혁이며 정상 데이터이므로(edge-case #18) 법령ID로
        묶으면 같은 날 두 번 공포된 버전 중 하나가 사라진다.

    트레이드오프:
        법령ID나 MST가 빈 항목은 조용히 건너뛴다. 예외를 던지지 않는 이유는 그
        항목이 우리 대상인지조차 알 수 없기 때문이다 — 대상 여부를 모르는 것에
        실행을 멈출 근거가 없다. 대신 경고 로그를 남긴다. spec 경로가 통째로
        틀렸다면 교집합이 0이 되어 `SUCCEEDED_ZERO`가 연속되고, 연속 0건 알람이
        그것을 잡는다.

    엣지 케이스:
        - spec에 `law_id_path`가 없는 계열: 빈 튜플. 일자별 이력이 아니면 이
          함수를 쓸 수 없다.
        - 같은 MST가 한 응답에 두 번: 그대로 두 번 돌려준다. 중복 제거는 호출부가
          창 전체 기준으로 한다.
    """
    spec = collection.spec
    if not (spec.law_id_path and spec.identity_path):
        return ()

    detected: list[DetectedLaw] = []
    for item in collection.items:
        law_id = (item.findtext(spec.law_id_path) or "").strip()
        mst = (item.findtext(spec.identity_path) or "").strip()
        if not law_id or not mst:
            _log.warning("ops.item_missing_identity", reg_date=reg_date, law_id=law_id, mst=mst)
            continue
        if law_id not in law_ids:
            continue
        detected.append(
            DetectedLaw(
                reg_date=reg_date,
                law_id=law_id,
                law_name=_text_or_none(item, LAW_NAME_PATH),
                mst=mst,
            )
        )
    return tuple(detected)


def _text_or_none(item: Element, path: str) -> str | None:
    """경로의 텍스트를 돌려주되 비어 있으면 None. 빈 문자열과 부재를 섞지 않는다."""
    value = (item.findtext(path) or "").strip()
    return value or None


async def previous_change_set(conn: psycopg.AsyncConnection[Any], mst: str) -> UUID | None:
    """이 MST를 `to` 쪽으로 하는 `change_set`이 이미 있으면 그 id를 돌려준다."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT cs.id
              FROM change_set cs
              JOIN regulation_document d ON d.id = cs.to_document_id
             WHERE d.mst = %s
             LIMIT 1
            """,
            (mst,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, UUID) else UUID(str(value))


async def processed_state(conn: psycopg.AsyncConnection[Any], mst: str) -> tuple[bool, UUID | None]:
    """(이미 처리했는가, 그때 만든 change_set id). 한 번의 조회로 둘 다 답한다.

    목적:
        멱등성의 판정 지점. 여기서 True 면 이 MST 에 대해 API 호출이 한 번도
        일어나지 않는다.

    구현 이유:
        **두 곳을 본다.** `change_set` 존재와 `ops_law_outcome`의 종결 처분이다.
        전자만 보면 제정본(`NO_PREVIOUS`)이 영원히 미처리로 남아 재확인 창이
        도는 동안 매일 `oldAndNew`를 다시 부른다. 후자만 보면 `regchange diff auto`로
        손수 돌린 비교가 보이지 않아 다시 처리된다 — 운영 이력에 없지만 결과는 있다.

        판정과 id 를 한 함수가 돌려주는 이유: 호출부가 "처리됐다"와 "그 결과가
        무엇이었나"를 따로 물으면 두 질문 사이에 상태가 달라질 수 있고, 그때
        기록되는 행은 어느 쪽도 아닌 값이 된다.

    트레이드오프:
        `FAILED`는 종결로 보지 않으므로 실패한 법령은 다음 실행이 다시 시도한다.
        재시도 상한을 두지 않았다 — **기록이 우선이고 재시도 강화는 하지 않기로**
        했다. 같은 실패가 반복되면 `ops history`에 같은 사유가 며칠 연속 남고,
        그것이 사람이 봐야 한다는 신호다.

    엣지 케이스:
        - 제정본으로 종결된 MST: `(True, None)`. change_set 이 없는 정상 종결이다.
        - `SKIPPED_DONE` 행만 있는 MST: 종결로 보지 않는다. 그 행 자체가 다른
          종결 행의 존재를 전제하므로 순환하지 않는다.
    """
    change_set_id = await previous_change_set(conn, mst)
    if change_set_id is not None:
        return True, change_set_id
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
              FROM ops_law_outcome
             WHERE mst = %s AND status IN ('DIFFED', 'NO_PREVIOUS')
             LIMIT 1
            """,
            (mst,),
        )
        return await cur.fetchone() is not None, None


async def already_processed(conn: psycopg.AsyncConnection[Any], mst: str) -> bool:
    """이 MST를 이미 처리했는가. `processed_state`의 판정만 필요한 호출부용이다."""
    processed, _ = await processed_state(conn, mst)
    return processed


async def _process_law(
    conn: psycopg.AsyncConnection[Any],
    client: LawApiClient,
    store: DocumentStore,
    masker: Masker,
    detected: DetectedLaw,
    *,
    run_id: str,
    now: dt.datetime,
) -> LawOutcome:
    """법령 버전 하나를 처리한다. 실패해도 예외를 올리지 않고 값으로 돌려준다."""
    try:
        result = await autodiff(
            conn, client, store, masker, to_mst=detected.mst, run_id=run_id, now=now
        )
    # 광범위한 except 인 이유: 여기서 잡는 것은 "이 법령을 처리하지 못했다"는 사실
    # 하나이며, 원인의 종류(네트워크·파싱·제약 위반)는 이 층위의 분기 대상이 아니다.
    # **삼키지 않는다** — 사유를 DB 행과 구조화 로그 양쪽에 남기고, 실행 상태는
    # PARTIAL 이 된다 (CLAUDE.md §4 의 (b) 경로).
    except Exception as exc:
        await conn.rollback()
        _log.exception(
            "ops.law_failed",
            reg_date=detected.reg_date,
            law_id=detected.law_id,
            mst=detected.mst,
            error_type=type(exc).__name__,
        )
        return LawOutcome(
            detected=detected,
            status=LawOutcomeStatus.FAILED,
            failure_detail=f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(result, AutoDiffOutcome):
        _log.info("ops.law_no_previous", law_id=detected.law_id, mst=detected.mst)
        return LawOutcome(detected=detected, status=LawOutcomeStatus.NO_PREVIOUS)

    change_set = result.change_set
    if not change_set.created or change_set.result is None:
        # 사전 조회를 통과했는데 여기서 기존 행을 만났다 — 같은 버전 쌍이 다른
        # 경로로 이미 만들어진 경우다. 새로 만들지 않았으므로 DIFFED 가 아니다.
        return LawOutcome(
            detected=detected,
            status=LawOutcomeStatus.SKIPPED_DONE,
            change_set_id=change_set.change_set_id,
            from_mst=result.previous.previous.mst,
        )

    counts = change_set.result.counts
    source, exceeded = await _change_set_flags(conn, change_set.change_set_id)
    return LawOutcome(
        detected=detected,
        status=LawOutcomeStatus.DIFFED,
        change_set_id=change_set.change_set_id,
        from_mst=result.previous.previous.mst,
        mst_resolution_source=source,
        change_ratio_exceeded=exceeded,
        articles_changed=counts.added + counts.deleted + counts.modified + counts.editorial,
    )


async def _change_set_flags(
    conn: psycopg.AsyncConnection[Any], change_set_id: UUID
) -> tuple[str | None, bool | None]:
    """`change_set`의 탐지 3·4 값을 읽는다 — **다시 계산하지 않고 읽기만 한다**.

    두 값 모두 `compute_change_set`이 파생시킨 것이며, 여기서 재계산하면 같은
    사실의 두 번째 출처가 생긴다. 두 출처가 갈리는 날 어느 쪽이 옳은지 알 수 없다.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT mst_resolution_source, change_ratio_exceeded FROM change_set WHERE id = %s",
            (change_set_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    return str(row[0]), bool(row[1])


def _probe_of(result: DailyIngestResult, reg_date: str, matched: int) -> DateProbe:
    """폴링 결과를 실행 기록용 값으로 옮긴다."""
    total = None if result.collection is None else result.collection.total_count
    return DateProbe(
        reg_date=reg_date,
        status=result.status,
        total_count=total,
        matched=matched,
        detail=result.detail,
    )


async def run_daily(
    conn: psycopg.AsyncConnection[Any],
    client: LawApiClient,
    store: DocumentStore,
    masker: Masker,
    *,
    corpus: CorpusConfig,
    dates: tuple[str, ...],
    run_id: str,
    now: dt.datetime,
) -> DailyRunResult:
    """하루 실행 전체를 돈다. 예외를 올리지 않고 결과 값으로 돌려준다.

    목적:
        카나리아 → 날짜별 폴링 → 코퍼스 필터 → 법령별 diff → 실행 결과 집계.

    구현 이유:
        **DB 기록을 여기서 하지 않는다.** 이 함수는 결과 값을 만들고, 기록은
        `ops.record.record_run`이 한다. 나눈 이유는 기록이 실패해도 실행 결과가
        표준출력에 남아야 하고(운영자가 그 자리에서 볼 수 있어야 한다), 반대로
        기록 로직을 테스트할 때 API를 때리지 않아야 하기 때문이다.

        카나리아를 실행당 한 번만 부른다. 카나리아가 답하는 질문("파이프라인이
        살아 있는가")은 실행 단위이지 날짜 단위가 아니다.

        **`started_at`/`finished_at`은 주입된 `now`가 아니라 벽시계다.** 두 축을
        구별한다 — `now`는 적재·비교에 쓰이는 **논리 시각**(bitemporal `known_from`)
        이고, 실행 이력의 두 시각은 **얼마나 걸렸는가**를 말한다. `now`로 둘 다
        채우면 소요 시간이 항상 0이 되어 "요즘 실행이 길어지고 있다"는 신호가
        관측되지 않는다. 운영에서 두 값은 같은 시각이지만, 테스트가 논리 시각을
        고정해도 소요 시간 기록이 깨지지 않는다.

    트레이드오프:
        날짜 하나를 끝낼 때까지 그 날짜의 `Collection`을 들고 있다. 스냅샷 보관
        여부를 "새 MST가 있는가"로 정하므로 판정 전에 버릴 수 없다. 창 전체를
        모으지는 않으므로 메모리 상한은 최악의 하루(법령 1,464건 ≈ 2.7MB)다.

        **예외를 전부 값으로 바꾸지는 않는다.** 법령 단위 실패는 값이 되지만,
        저장소·DB 같은 기반 실패는 그대로 올라간다. 그 둘을 같이 삼키면 "0건
        성공"으로 보이는 실행이 만들어질 수 있다 — 호출부가 그 예외를 받아
        실패한 실행으로 기록한다.

    엣지 케이스:
        - 카나리아 실패: 폴링하지 않는다. `probes`가 비고 상태는
          `SKIPPED_CANARY_FAILED`다.
        - 날짜 하나가 실패: 나머지 날짜는 계속 폴링한다. 상태는 `PARTIAL`이다.
        - 창 안 중복 MST: 첫 등장 날짜로 한 번만 처리한다. 두 번 처리하면
          `resolve_previous_mst`가 같은 스냅샷 디렉터리를 두 번 쓰려다 실패한다.
        - `dates`가 비어 있음: 폴링 없이 `SUCCEEDED_ZERO`. 호출부가
          `lookback_dates`를 쓰면 발생하지 않는다.
    """
    started_at = utc_now()
    law_ids = frozenset(corpus.active_law_ids)

    canary = await probe_canary(client)
    if not canary.passed:
        return _result(
            run_id=run_id,
            started_at=started_at,
            lookback_days=len(dates),
            dates=dates,
            canary=canary,
            probes=(),
            outcomes=(),
            client_stats=(0, 0),
            detail=canary.detail,
        )

    probes: list[DateProbe] = []
    pending: list[tuple[DetectedLaw, bool, UUID | None]] = []
    seen: set[str] = set()
    requests = 0
    retries = 0

    for reg_date in dates:
        result = await poll_promulgation_date(client, reg_date, canary=canary)
        collection = result.collection
        if collection is not None:
            requests += collection.stats.requests
            retries += collection.stats.retries

        detected = () if collection is None else extract_detected(collection, reg_date, law_ids)
        probes.append(_probe_of(result, reg_date, len(detected)))

        fresh = [law for law in detected if law.mst not in seen]
        seen.update(law.mst for law in fresh)
        if collection is None or not fresh:
            continue

        # 처분을 여기서 한 번만 판정한다. 스냅샷 보관 여부와 법령별 처분이 같은
        # 판정에서 나와야 둘이 어긋나지 않는다.
        decided = [(law, *await processed_state(conn, law.mst)) for law in fresh]

        # 새로 처리할 MST 가 있는 날짜만 폴링 응답을 보관한다 (모듈 docstring 참조).
        if any(not processed for _, processed, _ in decided):
            await write_snapshot(
                store,
                collection,
                run_id=run_id,
                fetched_at=now,
                params={"regDt": reg_date},
                display=PAGE_SIZE,
                masker=masker,
            )
        pending.extend(decided)

    outcomes: list[LawOutcome] = []
    for law, processed, existing in pending:
        if processed:
            _log.info("ops.law_skipped_done", law_id=law.law_id, mst=law.mst)
            outcomes.append(
                LawOutcome(
                    detected=law,
                    status=LawOutcomeStatus.SKIPPED_DONE,
                    change_set_id=existing,
                )
            )
            continue
        outcomes.append(
            await _process_law(conn, client, store, masker, law, run_id=run_id, now=now)
        )

    return _result(
        run_id=run_id,
        started_at=started_at,
        lookback_days=len(dates),
        dates=dates,
        canary=canary,
        probes=tuple(probes),
        outcomes=tuple(outcomes),
        client_stats=(requests, retries),
        detail=_summarize(probes, outcomes),
    )


def _summarize(probes: list[DateProbe], outcomes: list[LawOutcome]) -> str:
    """사람이 한 줄로 읽는 요약. 실패 건수를 앞에 둔다."""
    failed_dates = sum(1 for probe in probes if probe.failed)
    failed_laws = sum(1 for outcome in outcomes if outcome.status is LawOutcomeStatus.FAILED)
    diffed = sum(1 for outcome in outcomes if outcome.status is LawOutcomeStatus.DIFFED)
    skipped = sum(1 for outcome in outcomes if outcome.status is LawOutcomeStatus.SKIPPED_DONE)
    no_previous = sum(1 for outcome in outcomes if outcome.status is LawOutcomeStatus.NO_PREVIOUS)
    return (
        f"날짜 {len(probes)}건(실패 {failed_dates}) / 대상 법령 {len(outcomes)}건 "
        f"(diff {diffed} · 기처리 {skipped} · 제정 {no_previous} · 실패 {failed_laws})"
    )


def _result(
    *,
    run_id: str,
    started_at: dt.datetime,
    lookback_days: int,
    dates: tuple[str, ...],
    canary: CanaryResult,
    probes: tuple[DateProbe, ...],
    outcomes: tuple[LawOutcome, ...],
    client_stats: tuple[int, int],
    detail: str,
) -> DailyRunResult:
    """결과 값을 조립한다. 상태는 `derive_status`가 정한다 — 여기서 주장하지 않는다.

    `finished_at`을 이 안에서 읽는 이유는 조립 지점이 곧 종료 지점이기 때문이다.
    호출부가 넘기게 두면 시각을 언제 읽었는지가 호출부마다 달라진다.
    """
    requests, retries = client_stats
    return DailyRunResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now(),
        status=derive_status(canary_passed=canary.passed, probes=probes, outcomes=outcomes),
        lookback_days=lookback_days,
        target_dates=dates,
        canary=canary,
        probes=probes,
        outcomes=outcomes,
        requests=requests,
        retries=retries,
        detail=detail,
    )
