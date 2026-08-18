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
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from regchange.diff import (
    CANDIDATE_POOL_WARN_SIZE,
    ArticleSnapshot,
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


@dataclass(frozen=True, slots=True)
class _DocumentHeader:
    id: UUID
    law_id: str
    promulgation_date: dt.date
    revision_kind: str | None


async def _read_document(conn: psycopg.AsyncConnection[Any], document_id: UUID) -> _DocumentHeader:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, law_id, promulgation_date, revision_kind
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


async def compute_change_set(
    conn: psycopg.AsyncConnection[Any],
    *,
    from_document_id: UUID,
    to_document_id: UUID,
    now: dt.datetime,
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

    change_set_id = uuid4()
    await _write(
        conn,
        change_set_id=change_set_id,
        source=source,
        target=target,
        result=result,
        now=now,
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
                candidate_pool_size
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
