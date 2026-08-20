"""change_set 계산과 적재 — 두 버전을 읽어 diff 하고 결과를 쓴다.

목적:
    DB 에 적재된 두 버전을 읽어 `regchange.diff` 에 넘기고, 나온 변경과 이동 후보를
    `change_set` 단위 트랜잭션으로 저장한다.

구현 이유:
    판정 자체는 `diff` 패키지가 하고 이 모듈은 읽기·쓰기만 한다. 원칙 1이 요구하는
    분리이며(diff 는 I/O 를 갖지 않는다), import 계약이 그 방향을 강제한다.

    **DB 행을 `ArticleSnapshot` 으로 좁혀 넘긴다.** 파서 결과도 같은 스냅샷으로
    좁혀지므로(`diff.snapshots.snapshot_from_unit`), 픽스처로 검증한 판정이 DB
    경로에서도 같은 코드를 탄다. 두 경로가 다른 함수를 타면 픽스처 테스트가
    운영 동작을 보증하지 못한다.

    트랜잭션 경계는 `change_set` 하나다. 작업 3이 문서 단위로 끊은 것과 같은
    발상이며, 부분 상태가 의미 있는 단위로만 존재한다.

트레이드오프:
    두 버전의 조문을 전부 메모리에 올린다. 관측 최대가 자본시장법 682조문이므로
    문제되지 않는다. 스트리밍으로 바꾸면 이동 후보 생성이 전체 집합을 필요로 하므로
    어차피 한 번은 모아야 한다.

    `article_change` 에 UNCHANGED 행을 만들지 않는다. 따라서 "이 조문을 검사했다"를
    행으로 증명할 수 없고, 대신 `change_set` 의 건수 CHECK 가 전수 검사를 증명한다.

엣지 케이스:
    - 같은 두 버전을 다시 계산: 유니크 인덱스가 막는다. 중복 제거가 아니라 건너뛰기로
      처리하고 건너뛴 사실을 결과에 남긴다 (edge-case #18).
    - 두 문서의 `law_id` 가 다름: `DiffError`. 다른 법령을 비교하면 전 조문이
      ADDED/DELETED 로 잡히고, 그 결과는 그럴듯해 보인다.
    - `from` 이 `to` 보다 나중에 공포됨: 실패시킨다. 날짜 창이 역전되어 이동 표기가
      전부 창 밖으로 빠지는데, 그것이 "이동 없음"으로 보인다.
    - 후보 풀이 경고 크기를 넘음: 로그로 남기고 진행한다. 잘라내지 않는다 —
      조용한 절단은 이 저장소가 반복해서 막아 온 형태다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from regchange.diff import (
    CANDIDATE_POOL_WARN_SIZE,
    ArticleSnapshot,
    DiffCounts,
    DiffError,
    DiffResult,
    MoveWindow,
    diff_versions,
)
from regchange.parse.models import AmendmentMarker, MoveReference

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChangeSetOutcome:
    """계산 결과. 새로 만들었는지 건너뛰었는지를 값으로 구별한다."""

    change_set_id: UUID
    created: bool
    """False 면 같은 버전 쌍이 이미 있어 건너뛴 것이다. 실패가 아니다."""

    result: DiffResult | None
    """건너뛴 경우 None. 기존 행을 다시 계산해 비교하지 않는다."""


CHANGE_RATIO_WARN = 0.5
"""변경 조문 비율 경고 임계값 — (added+deleted+modified) / max(from_count, to_count).

**0.5로 정한 근거는 실측 분포다.** 12개월 전수에서 `일부개정` 2,097건의 조문 이벤트가
10,663건이므로 개정 1건당 평균 5조문이다 (`amendment-frequency.md` D-2). 코퍼스에서
가장 큰 자체 개정도 정보통신망법 26조문 / 조문단위 181개(약 14%), 개인정보 보호법
19조문 / 141개(약 13%)였다. **실제 일부개정은 0.5에 한참 못 미친다.**

반대편 끝은 1.0이다 — 다른 법령을 비교하면 전 조문이 ADDED/DELETED로 잡히므로 비율이
1.0에 가깝다. 0.5는 두 분포 사이의 빈 구간이며, 어느 쪽으로도 여유가 크다.

**실패가 아니라 경고다.** `전부개정`은 정상적으로 이 값을 넘고, `일괄개정`(12개월
597조문)도 넘을 수 있다. 넘었다는 사실만 기록하고 판단은 사람이 한다 — 모르는 상황에서
차단부터 걸지 않는다는 이 저장소의 규칙(ADR-009의 미지 부처명과 같은 논리)을 따른다.
"""

FULL_AMENDMENT_KIND = "전부개정"
"""이 `제개정구분명` 이면 변경 규모 판정을 하지 않는다. 전부 바뀌는 것이 정의다."""


class MstResolutionSource(StrEnum):
    """`from_mst` 를 어떻게 골랐는가 — **호출부가 주장하지 않고 파생된다**.

    목적:
        비교 짝의 출처를 값으로 남겨, 나중에 "이 diff 가 왜 이 두 버전을 비교했는가"에
        답할 수 있게 한다.

    구현 이유:
        호출부가 이 값을 직접 넘기지 않는다. `resolved_from_mst` 와 실제 `from` 문서의
        MST 를 대조해 **계산한다.** 호출부가 넘기게 두면 "RESOLVED 라고 적어 놓고 다른
        MST 를 쓰는" 상태가 만들어질 수 있고, 그것은 이 장치가 막으려는 바로 그 실패다.

    트레이드오프:
        자동 확보를 시도하지 않은 경우(`resolved_from_mst is None`)와 사람이 지정한
        경우가 둘 다 `MANUAL` 로 합쳐진다. 나누려면 값이 하나 더 필요한데, 실제로 그
        둘을 구별해 할 일이 다르지 않다 — 어느 쪽이든 자동 확보 근거가 없다.

    엣지 케이스:
        - `MISMATCH` 는 **실패가 아니다.** 수동 지정 경로를 유지하기로 했으므로
          불일치가 정상적으로 발생한다(골든셋 재현). 조용히 넘기지 않으려고 값으로 남긴다.
    """

    RESOLVED = "RESOLVED"
    """`oldAndNew` 가 알려준 직전 MST 를 그대로 썼다."""

    MANUAL = "MANUAL"
    """사람이 지정했고 자동 확보값이 없다."""

    MISMATCH = "MISMATCH"
    """자동 확보값과 실제 사용값이 다르다. 로그와 이 컬럼에 남는다."""


class FromDocumentSource(StrEnum):
    """직전 버전 본문을 재사용했는가 다시 받았는가."""

    REUSED = "REUSED"
    REFETCHED = "REFETCHED"


class ReuseSkipReason(StrEnum):
    """재사용하지 않은 이유. `REFETCHED` 일 때만 의미가 있다.

    `SHA256_MISMATCH` 는 다른 셋과 성질이 다르다 — 나머지는 "없어서 다시 받았다"이고
    이것은 **"있는데 어긋났다"**이다. 저장된 파일이 변조됐거나 기록이 틀렸거나
    둘 중 하나이며 어느 쪽이든 사건이다. 재수집으로 덮지 않고 남긴다.
    """

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    NO_DOCUMENT = "NO_DOCUMENT"
    NO_SNAPSHOT = "NO_SNAPSHOT"
    SHA256_MISMATCH = "SHA256_MISMATCH"


@dataclass(frozen=True, slots=True)
class PairProvenance:
    """비교 짝이 어떻게 만들어졌는가. `compute_change_set` 이 그대로 기록한다.

    목적:
        짝 선택의 근거를 diff 결과와 같은 행에 남긴다.

    구현 이유:
        `mst_resolution_source` 를 여기 넣지 않았다. 그것은 이 값과 실제 문서로부터
        **파생**되며, 넣는 순간 호출부가 주장할 수 있게 된다 (`MstResolutionSource`
        docstring 참조).

    트레이드오프:
        기본값이 "아무것도 시도하지 않음"이다. 수동 경로가 이 값을 넘기지 않아도
        동작해야 하기 때문이다 — 골든셋 재현과 테스트가 그 경로를 쓴다.

    엣지 케이스:
        - `from_document_source` 가 None 이면 재사용 판정을 아예 하지 않은 것이다.
          `REFETCHED` 와 구별된다 — 후자는 "판정했고 재수집했다"이다.
    """

    resolved_from_mst: str | None = None
    from_document_source: FromDocumentSource | None = None
    reuse_skip_reason: ReuseSkipReason | None = None


NO_PROVENANCE = PairProvenance()
"""자동 확보를 시도하지 않은 경우의 기본값. 수동 지정 경로가 이것을 쓴다."""


@dataclass(frozen=True, slots=True)
class _DocumentHeader:
    id: UUID
    law_id: str
    mst: str
    promulgation_date: dt.date
    revision_kind: str | None


async def _read_document(conn: psycopg.AsyncConnection[Any], document_id: UUID) -> _DocumentHeader:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, law_id, mst, promulgation_date, revision_kind
              FROM regulation_document
             WHERE id = %s AND known_until = 'infinity'
            """,
            (document_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise DiffError(f"문서를 찾을 수 없다: {document_id}")
    if row["promulgation_date"] is None:
        raise DiffError(
            f"공포일자가 없는 문서다: {document_id}. 이동 표기의 날짜 창을 정할 수 없다"
        )
    return _DocumentHeader(
        id=row["id"],
        law_id=row["law_id"],
        mst=row["mst"],
        promulgation_date=row["promulgation_date"],
        revision_kind=row["revision_kind"],
    )


def _marker_signature_from_jsonb(markers: list[dict[str, Any]]) -> tuple[str, ...]:
    """저장된 마커 jsonb 를 파서 경로와 **같은 서명**으로 편다.

    `AmendmentMarker` 로 되살린 뒤 `diff.snapshots.marker_signature` 를 쓴다.
    문자열을 여기서 직접 조립하면 두 경로의 서명이 미묘하게 달라지고, 그러면
    EDITORIAL 판정이 출처에 따라 달라진다.
    """
    from regchange.diff.snapshots import marker_signature

    return marker_signature(tuple(AmendmentMarker.model_validate(m) for m in markers))


async def _read_snapshots(
    conn: psycopg.AsyncConnection[Any], document_id: UUID
) -> tuple[ArticleSnapshot, ...]:
    """비교 대상 조문을 읽는다. `HEADING` 은 조문이 아니므로 제외한다 (ADR-001)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, article_no, branch_no, title,
                   body_norm, body_norm_sha256, body_markers, moves, reference_raw
              FROM regulation_article
             WHERE document_id = %s AND known_until = 'infinity' AND unit_type = 'ARTICLE'
             ORDER BY article_no, branch_no
            """,
            (document_id,),
        )
        rows = await cur.fetchall()

    return tuple(
        ArticleSnapshot(
            article_id=row["id"],
            article_no=row["article_no"],
            branch_no=row["branch_no"],
            title=row["title"],
            body_norm=row["body_norm"],
            body_norm_sha256=row["body_norm_sha256"],
            marker_signature=_marker_signature_from_jsonb(row["body_markers"]),
            moves=tuple(MoveReference.model_validate(m) for m in row["moves"]),
            reference_raw=row["reference_raw"],
        )
        for row in rows
    )


async def _existing_change_set(
    conn: psycopg.AsyncConnection[Any], from_id: UUID, to_id: UUID
) -> UUID | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM change_set WHERE from_document_id = %s AND to_document_id = %s",
            (from_id, to_id),
        )
        row = await cur.fetchone()
    return None if row is None else UUID(str(row[0]))


def resolve_mst_source(
    *, resolved_from_mst: str | None, actual_from_mst: str
) -> MstResolutionSource:
    """자동 확보값과 실제 사용값을 대조해 출처를 판정한다.

    목적:
        §3-3 연속성 검증. "자동으로 찾은 직전 MST"와 "실제로 diff에 쓴 from MST"가
        같은지 본다.

    구현 이유:
        **이것이 없으면 나중에 "왜 이 diff가 이상하지"를 추적할 방법이 없다.**
        두 단계 건너뛴 버전과 비교해도 예외가 나지 않고 결과는 그럴듯하다. 유일한
        단서가 "우리가 찾은 것과 우리가 쓴 것이 다르다"는 사실이다.

    트레이드오프:
        불일치를 실패로 만들지 않는다. 수동 지정 경로를 유지하기로 했으므로(골든셋
        재현·테스트) 불일치가 정상적으로 발생한다. 대신 값과 로그로 남긴다 —
        차단하지 못하는 대신 추적 가능하게 했다.

    엣지 케이스:
        - `resolved_from_mst is None`: 자동 확보를 시도하지 않았다. `MANUAL`.
        - 두 값이 같다: `RESOLVED`.
        - 다르다: `MISMATCH`. **호출부가 이 값을 무시해도 DB에 남는다.**
    """
    if resolved_from_mst is None:
        return MstResolutionSource.MANUAL
    if resolved_from_mst == actual_from_mst:
        return MstResolutionSource.RESOLVED
    return MstResolutionSource.MISMATCH


def change_ratio(counts: DiffCounts) -> float:
    """변경 조문 비율. 분모는 두 버전의 조문 수 중 큰 쪽이다.

    분모를 큰 쪽으로 잡는 이유: 조문 수가 크게 다른 두 문서를 비교하면 작은 쪽을
    분모로 썼을 때 비율이 1.0을 넘어 의미를 잃는다. 큰 쪽은 보수적이다 — 같은
    상황에서 비율이 낮게 나오므로 **경고가 덜 뜨는 방향**이며, 거짓 경고보다
    놓친 경고가 비싼 이 도메인에서는 뒤집는 편이 나을 수도 있다. 지금은 실측
    분포(§CHANGE_RATIO_WARN)의 여유가 크므로 보수적인 쪽을 택했다.

    엣지 케이스:
        - 두 문서 다 조문 0건: 0.0. 나눗셈을 하지 않는다. 파서가 0건 문서를 막으므로
          정상 경로에서는 오지 않는다.
    """
    denominator = max(counts.from_article_count, counts.to_article_count)
    if denominator == 0:
        return 0.0
    return (counts.added + counts.deleted + counts.modified) / denominator


async def compute_change_set(
    conn: psycopg.AsyncConnection[Any],
    *,
    from_document_id: UUID,
    to_document_id: UUID,
    now: dt.datetime,
    provenance: PairProvenance = NO_PROVENANCE,
) -> ChangeSetOutcome:
    """두 버전을 비교해 `change_set` 을 만든다.

    목적:
        diff 결과를 감사 가능한 형태로 남긴다.

    구현 이유:
        멱등성을 **중복 제거가 아니라 건너뛰기**로 구현한다. 이미 있는 쌍이면
        계산조차 하지 않고 기존 id 를 돌려준다. 다시 계산해 비교하는 대안은
        "결과가 달라졌을 때 무엇이 맞는가"라는 답할 수 없는 질문을 만든다 —
        원칙 6에 따라 과거 행을 고칠 수 없으므로 판단할 근거가 없다.

        날짜 창을 두 문서의 공포일자로 만든다. `from` 은 배타, `to` 는 포함이다.
        `to` 의 공포일에 일어난 이동은 이번 diff 의 대상이고, `from` 의 공포일에
        일어난 이동은 이전 diff 의 대상이기 때문이다.

    트레이드오프:
        같은 쌍을 다시 계산할 방법이 없다. 판정 로직이 바뀌어 재계산이 필요하면
        새 `change_set` 을 다른 경로로 만들어야 한다. 그 제약을 받아들이는 대신
        "언제 계산한 결과인가"가 흔들리지 않는다.

    엣지 케이스:
        - 이미 있는 쌍: `created=False`, `result=None`.
        - 다른 법령끼리 비교: `DiffError`.
        - 공포일자 역전: `DiffError`.
        - 조문 0건 문서: 파서가 이미 막으므로(ADR-005) 정상 경로에서는 오지 않는다.
          와도 건수 CHECK 가 성립하므로 빈 change_set 이 만들어진다.
    """
    source = await _read_document(conn, from_document_id)
    target = await _read_document(conn, to_document_id)

    if source.law_id != target.law_id:
        raise DiffError(
            f"다른 법령끼리 비교할 수 없다: {source.law_id} vs {target.law_id}. "
            "전 조문이 ADDED/DELETED 로 잡히고 그 결과는 그럴듯해 보인다"
        )
    if source.promulgation_date == target.promulgation_date:
        # 실패시키지 않는다. 그런 쌍을 비교해야 할 때 막히고, 그 경우의 올바른 처리가
        # 아직 정해지지 않았다 — 처리 방법을 모르는 상태에서 차단부터 걸지 않는다
        # (ADR-009 의 미지 부처명과 같은 논리). 대신 기록해 0건과 구별한다.
        logger.warning(
            "changeset.same_promulgation_date",
            extra={
                "law_id": source.law_id,
                "promulgation_date": str(source.promulgation_date),
                "note": "이동 표기 날짜 창이 비어 moves_in_window 가 0 이 된다",
            },
        )
    if source.promulgation_date > target.promulgation_date:
        raise DiffError(
            f"공포일자가 역전됐다: from {source.promulgation_date} > to "
            f"{target.promulgation_date}. 날짜 창이 뒤집혀 이동 표기가 전부 창 밖이 된다"
        )

    existing = await _existing_change_set(conn, from_document_id, to_document_id)
    if existing is not None:
        logger.info(
            "changeset.skipped", extra={"change_set_id": str(existing), "reason": "already_exists"}
        )
        return ChangeSetOutcome(change_set_id=existing, created=False, result=None)

    from_snapshots = await _read_snapshots(conn, from_document_id)
    to_snapshots = await _read_snapshots(conn, to_document_id)
    window = MoveWindow(after=source.promulgation_date, through=target.promulgation_date)
    result = diff_versions(from_snapshots, to_snapshots, window=window)

    if result.candidate_pool_size > CANDIDATE_POOL_WARN_SIZE:
        logger.warning(
            "changeset.candidate_pool_large",
            extra={
                "law_id": source.law_id,
                "pool_size": result.candidate_pool_size,
                "threshold": CANDIDATE_POOL_WARN_SIZE,
            },
        )

    mst_source = resolve_mst_source(
        resolved_from_mst=provenance.resolved_from_mst, actual_from_mst=source.mst
    )
    if mst_source is MstResolutionSource.MISMATCH:
        logger.warning(
            "changeset.mst_mismatch",
            extra={
                "law_id": source.law_id,
                "resolved_from_mst": provenance.resolved_from_mst,
                "actual_from_mst": source.mst,
                "to_mst": target.mst,
                "note": "자동 확보한 직전 MST 와 실제 비교에 쓴 MST 가 다르다",
            },
        )
    if provenance.reuse_skip_reason is ReuseSkipReason.SHA256_MISMATCH:
        logger.warning(
            "changeset.snapshot_sha256_mismatch",
            extra={
                "law_id": source.law_id,
                "from_mst": source.mst,
                "note": "저장된 스냅샷의 해시가 기록과 다르다. 재수집했으나 원인은 미확인 "
                "— docs/incidents/ 기록 후보다",
            },
        )

    ratio = change_ratio(result.counts)
    full_amendment = target.revision_kind == FULL_AMENDMENT_KIND
    ratio_exceeded = ratio > CHANGE_RATIO_WARN and not full_amendment
    if ratio_exceeded:
        logger.warning(
            "changeset.change_ratio_exceeded",
            extra={
                "law_id": source.law_id,
                "ratio": round(ratio, 4),
                "threshold": CHANGE_RATIO_WARN,
                "revision_kind": target.revision_kind,
                "note": "다른 법령을 비교했을 가능성을 먼저 의심한다 "
                "— 그 경우 비율이 1.0 에 가깝다",
            },
        )

    change_set_id = uuid4()
    await _write(
        conn,
        change_set_id=change_set_id,
        source=source,
        target=target,
        result=result,
        now=now,
        mst_source=mst_source,
        provenance=provenance,
        ratio_exceeded=ratio_exceeded,
    )
    return ChangeSetOutcome(change_set_id=change_set_id, created=True, result=result)


async def _write(
    conn: psycopg.AsyncConnection[Any],
    *,
    change_set_id: UUID,
    source: _DocumentHeader,
    target: _DocumentHeader,
    result: DiffResult,
    now: dt.datetime,
    mst_source: MstResolutionSource,
    provenance: PairProvenance,
    ratio_exceeded: bool,
) -> None:
    """한 트랜잭션으로 change_set 과 그 자식 행을 쓴다."""
    counts = result.counts
    counts.verify(context=f"change_set {change_set_id}")

    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO change_set (
                id, law_id, from_document_id, to_document_id, revision_kind,
                from_promulgation_date, to_promulgation_date, computed_at,
                from_article_count, to_article_count,
                added, deleted, modified, editorial, unchanged,
                moves_in_window, moves_out_of_window, out_of_window_dates,
                candidate_pool_size, same_promulgation_date,
                mst_resolution_source, resolved_from_mst,
                from_document_source, reuse_skip_reason, change_ratio_exceeded
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s)
            """,
            (
                change_set_id,
                target.law_id,
                source.id,
                target.id,
                target.revision_kind,
                source.promulgation_date,
                target.promulgation_date,
                now,
                counts.from_article_count,
                counts.to_article_count,
                counts.added,
                counts.deleted,
                counts.modified,
                counts.editorial,
                counts.unchanged,
                result.moves_in_window,
                result.moves_out_of_window,
                Jsonb(list(result.out_of_window_dates)),
                result.candidate_pool_size,
                source.promulgation_date == target.promulgation_date,
                mst_source.value,
                provenance.resolved_from_mst,
                None
                if provenance.from_document_source is None
                else provenance.from_document_source.value,
                None
                if provenance.reuse_skip_reason is None
                else provenance.reuse_skip_reason.value,
                ratio_exceeded,
            ),
        )

        for change in result.changes:
            await cur.execute(
                """
                INSERT INTO article_change (
                    id, change_set_id, change_type, from_article_id, to_article_id,
                    article_no, branch_no, priority_rank
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    change_set_id,
                    change.change_type.value,
                    change.from_article_id,
                    change.to_article_id,
                    change.article_no,
                    change.branch_no,
                    change.priority_rank,
                ),
            )

        for candidate in result.candidates:
            await cur.execute(
                """
                INSERT INTO article_move_candidate (
                    id, change_set_id,
                    from_article_no, from_branch_no, to_article_no, to_branch_no,
                    score, evidence_kind, evidence, cardinality
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    change_set_id,
                    candidate.from_ref[0],
                    candidate.from_ref[1],
                    candidate.to_ref[0],
                    candidate.to_ref[1],
                    candidate.score,
                    candidate.evidence_kind.value,
                    Jsonb(candidate.evidence),
                    candidate.cardinality.value,
                ),
            )
