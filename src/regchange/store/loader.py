"""스냅샷 → 파서 → 적재. 문서 단위 트랜잭션, 마지막에 완료 표시.

목적:
    매니페스트가 가리키는 페이지를 sha256 검증과 함께 읽어 파싱하고, 조문 단위로
    DB 에 적재한다. 무엇이 적재되고 무엇이 건너뛰어졌는지를 건수로 남긴다.

구현 이유:
    **트랜잭션 경계를 문서(MST)에 둔다.** 부분 상태가 존재하되 그 부분이 의미 있는
    단위로 끊긴다 — "문서 단위로는 완전, run 단위로는 미완료"다. 페이지 단위로
    쪼개면 반쯤 적재된 문서를 찾아내는 조회 경로와 재적재 규칙이 따로 필요해지고,
    run 전체를 한 트랜잭션으로 묶으면 문서 하나의 파싱 실패가 그날 전체를 되돌린다
    (2025-10-01 하루에 법령 1,464건이 관측됐다).

    **`load_run` 행을 마지막에 쓴다.** 그 존재가 곧 적재 완료를 의미한다. 중간에
    죽으면 행이 없으므로 불완전한 적재가 완전한 것으로 보이지 않는다. 스냅샷
    매니페스트를 마지막에 쓴 것과 같은 발상이다.

    **`valid_from` 을 채우지 않는다.** 본문 API 의 `조문시행일자` 는 문서 시행일로
    평탄화되어 있으므로(edge-case #8, ADR-005) 그 값을 `valid_from` 으로 승격하는
    경로를 아예 만들지 않았다. 문서 시행일은 `document_effective_date` 에만 들어가고
    조문의 `valid_from` 은 NULL, `valid_from_source` 는 `PENDING_HISTORY` 다.
    틀린 `valid_from` 이 한 행이라도 들어가면 원칙 6 때문에 지울 수 없고 정정
    이력만 남는다. 들어가지 않는 것이 지우는 것보다 낫다.

트레이드오프:
    문서마다 커밋하므로 run 이 중간에 죽으면 어느 load_run 에도 속하지 않는 고아
    문서가 DB 에 남는다. 매니페스트의 고아 페이지는 읽히지 않아 무해했지만 고아
    문서는 질의에 잡히므로 다르다. 그래서 `find_orphan_documents()` 로 찾을 수
    있게 했다 — 남는 것을 막는 대신 보이게 만들었다.

    멱등 검사를 위해 문서마다 SELECT 를 한 번 더 한다. INSERT ... ON CONFLICT DO
    NOTHING 이 더 빠르지만, 그러면 "이미 있어서 건너뛴 것"과 "내용이 달라 덮어써야
    하는 것"이 같은 무반응으로 뭉개진다. 그 구별이 이 작업의 요구사항이다.

엣지 케이스:
    - 같은 스냅샷 재적재: 문서와 조문이 모두 존재하고 내용이 같으면 전부 SKIPPED.
    - 문서는 있는데 조문 일부가 없음: 없는 것만 적재한다. 이전 run 이 중간에 죽은
      경우이며, 이 경로가 없으면 고아 문서를 손으로 지워야 한다.
    - 식별키는 같은데 정규화본 해시가 다름: `KeyConflictError`. 두 행의 전체 필드를
      예외에 담고 run 을 중단한다. 조용히 덮어쓰지 않는다 (edge-case #18).
    - 문서 시행일자가 없는 응답: `LoadError`. 버전을 식별할 수 없어 유니크 키가
      무너지므로 파서 결과를 그대로 통과시키지 않는다.
    - 루트가 `<법령>`이 아닌 페이지(행정규칙 등): 파서가 `ParseError` 를 던지고
      run 이 중단된다. 대상이 아닌 것을 적재하지 않는다 (ADR-006).
    - 소관부처가 마스터에 없음: 적재는 하되 `LOADED_UNRESOLVED` 로 세고
      `ministry_unresolved` 에 기록한다. 자동 등재하지 않는다 (ADR-009).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from regchange.adapters.storage import DocumentStore
from regchange.ingest.snapshot import Manifest, read_pages
from regchange.parse.assemble import assemble_body
from regchange.parse.law_xml import parse_law_document
from regchange.parse.models import ArticleUnit, LawDocument
from regchange.store.ministry import (
    MinistryMasterRow,
    MinistryObservation,
    Resolution,
    resolve,
)
from regchange.store.models import (
    Disposition,
    DocumentLoadResult,
    KeyConflictError,
    LoadCounts,
    LoadError,
    RunResult,
)

logger = logging.getLogger(__name__)

PENDING_HISTORY = "PENDING_HISTORY"
"""`valid_from` 미결합 상태를 나타내는 출처 값. 본문만 적재한 조문은 전부 이 값이다."""


async def fetch_ministry_master(
    conn: psycopg.AsyncConnection[Any],
) -> tuple[MinistryMasterRow, ...]:
    """마스터의 열린 행을 읽어 온다.

    `known_until = 'infinity'` 조건을 빠뜨리면 닫힌 과거 행이 섞여 같은 코드에
    이름이 둘 이상 나타나고, 해결이 비결정적이 된다.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT org_code, org_name, valid_from, valid_until
              FROM ministry_master
             WHERE known_until = 'infinity'
            """
        )
        rows = await cur.fetchall()
    return tuple(
        MinistryMasterRow(
            org_code=row["org_code"],
            org_name=row["org_name"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
        )
        for row in rows
    )


def _article_payload(unit: ArticleUnit) -> dict[str, Any]:
    """조문단위를 적재용 값으로 편다. 원문·정규화본·조립본·마커를 모두 보존한다.

    조립본(`body_norm`)은 `parse.assemble.assemble_body()` 가 만든다. 적재가 직접
    이어붙이지 않는 이유는 조립 규칙이 두 곳으로 갈리지 않게 하기 위해서다 —
    검색된 텍스트와 diff 한 텍스트가 달라지면 인용이 가리키는 것과 변경 판정의
    대상이 어긋나고, 그 어긋남은 예외를 내지 않는다.
    """
    body = assemble_body(unit)
    return {
        "body_norm": body.norm,
        "body_norm_sha256": body.sha256,
        "body_markers": [marker.model_dump(mode="json") for marker in body.markers],
        "reference_raw": unit.reference_raw,
        "moves": [move.model_dump(mode="json") for move in unit.moves],
        "article_key": unit.article_key,
        "seq_in_doc": unit.seq_in_doc,
        "unit_type": unit.unit_type.value,
        "article_no": unit.article_no,
        "branch_no": unit.branch_no,
        "title": unit.title,
        "text_raw": unit.content.raw,
        "text_norm": unit.content.norm,
        "text_norm_sha256": unit.content.sha256,
        "norm_rule_version": unit.content.rule_version,
        "amendment_markers": [marker.model_dump(mode="json") for marker in unit.content.markers],
        "body": [hang.model_dump(mode="json") for hang in unit.hangs],
        "heading_path": list(unit.heading_path),
    }


MST_PARAM = "MST"
"""본문 계열의 필수 요청 파라미터. 버전 식별자(법령일련번호)다.

**본문 응답 본문에는 MST 가 없다.** 픽스처 13개 전부에서 `법령일련번호` 태그가
0건이고, 루트 속성은 `법령키="0092442023071819563"`(법령ID + 공포일자 + 공포번호)로
MST 와 다른 값이다. 그래서 MST 는 응답이 아니라 **요청**에서 온다 —
`LAW_DOCUMENT.required_params` 가 `{"MST"}` 이고 스냅샷 매니페스트가 그 요청을
그대로 기록한다.

루트 속성에서 잘라 쓰지 않는 이유: 구성 규칙을 실측으로 확인하지 않았다.
조문키는 재구성해 원본과 대조한 뒤에야 규칙을 확정했다(ADR-001, 1,189조문 불일치
0건). 같은 절차 없이 문자열을 자르면 그것이 다음 사건의 발생지가 된다.
"""


async def _existing_document(
    cur: psycopg.AsyncCursor[Any],
    document: LawDocument,
    mst: str,
) -> dict[str, Any] | None:
    await cur.execute(
        """
        SELECT id, law_name, law_kind, ministry_code, ministry_name_observed, promulgation_date
          FROM regulation_document
         WHERE law_id = %s AND mst = %s AND document_effective_date = %s
           AND known_until = 'infinity'
        """,
        (document.law_id, mst, document.document_effective_date),
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None


def mst_of_manifest(manifest: Manifest) -> str:
    """매니페스트의 요청 파라미터에서 MST 를 꺼낸다.

    엣지 케이스:
        - `MST` 가 없는 매니페스트: `LoadError`. 본문 계열이 아닌 스냅샷을 본문
          적재 경로에 넣은 것이므로, 대체값을 지어내지 않고 실패시킨다.
    """
    mst = manifest.params.get(MST_PARAM)
    if not mst:
        raise LoadError(
            f"매니페스트에 {MST_PARAM} 가 없다 (target={manifest.target}). "
            "본문 응답에는 MST 가 없으므로 요청 파라미터가 유일한 출처다"
        )
    return mst


async def load_document(
    conn: psycopg.AsyncConnection[Any],
    document: LawDocument,
    *,
    mst: str,
    load_run_id: UUID,
    source_key: str,
    source_run_id: str,
    page_sha256: str,
    master: tuple[MinistryMasterRow, ...],
    now: dt.datetime,
) -> DocumentLoadResult:
    """문서 하나를 한 트랜잭션으로 적재한다.

    목적:
        문서와 그 조문을 원자적으로 적재하고 처분별 건수를 돌려준다.

    구현 이유:
        소관부처 해결 시점을 `now` 로 잡는다. 문서 시행일로 잡지 않는 이유는,
        마스터의 이름이 관측일부터 유효하고 문서가 관측한 이름도 같은 평탄화된
        현재 이름이기 때문이다. 문서 시행일로 조회하면 과거 문서가 전부 미해결이
        되는데, 그것은 "코드가 등재되지 않았다"가 아니라 "그 시점 이름을 모른다"를
        뜻하며 서로 다른 질문이다. 후자는 `ministry.name_at()` 이 답한다.

    트레이드오프:
        조문마다 개별 INSERT 를 한다. executemany 나 COPY 가 빠르지만, 어느
        조문에서 충돌했는지를 행 단위로 알아야 두 행의 전체 필드를 남길 수 있다.
        조문 수가 문서당 최대 682건(자본시장법)이므로 이 비용은 감당 가능하다.

    엣지 케이스:
        - 문서가 이미 있고 필드가 다름: `KeyConflictError`.
        - 조문이 이미 있고 정규화본 해시가 같음: `SKIPPED`.
        - 조문이 이미 있고 해시가 다름: `KeyConflictError`.
        - 문서 시행일자 없음: `LoadError`.
    """
    if document.document_effective_date is None:
        raise LoadError(
            f"문서 시행일자가 없다 (법령ID {document.law_id}). 버전을 식별할 수 없어 "
            "식별키가 무너진다. 파서 결과를 그대로 통과시키지 않는다"
        )

    observation = MinistryObservation(
        code_field=document.ministry_code,
        name_field=document.ministry,
    )
    resolution = resolve(observation, master, at=now.date())

    counts = LoadCounts(parsed_units=len(document.units))

    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        existing = await _existing_document(cur, document, mst)
        if existing is None:
            document_id = uuid4()
            await cur.execute(
                """
                INSERT INTO regulation_document (
                    id, law_id, mst, law_name, law_kind,
                    ministry_code, ministry_name_observed, revision_kind,
                    promulgation_date, document_effective_date,
                    source_key, source_run_id, source_page_sha256,
                    load_run_id, known_from
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    document.law_id,
                    mst,
                    document.law_name,
                    document.law_kind,
                    observation.code_field,
                    document.ministry,
                    document.revision_kind,
                    document.promulgation_date,
                    document.document_effective_date,
                    source_key,
                    source_run_id,
                    page_sha256,
                    load_run_id,
                    now,
                ),
            )
        else:
            document_id = existing["id"]
            _reject_document_mismatch(existing, document, observation)

        if not resolution.resolved:
            await _record_unresolved(
                cur,
                resolution=resolution,
                observation=observation,
                document_id=document_id,
                load_run_id=load_run_id,
                now=now,
            )

        loaded_disposition = (
            Disposition.LOADED if resolution.resolved else Disposition.LOADED_UNRESOLVED
        )

        for unit in document.units:
            payload = _article_payload(unit)
            await cur.execute(
                """
                SELECT id, text_norm_sha256, body_norm_sha256, unit_type, article_no, branch_no
                  FROM regulation_article
                 WHERE document_id = %s AND article_key = %s AND seq_in_doc = %s
                   AND known_until = 'infinity'
                """,
                (document_id, payload["article_key"], payload["seq_in_doc"]),
            )
            found = await cur.fetchone()
            if found is not None:
                if found["body_norm_sha256"] != payload["body_norm_sha256"]:
                    raise KeyConflictError(
                        key=(str(document_id), payload["article_key"], payload["seq_in_doc"]),
                        existing=dict(found),
                        incoming=payload,
                    )
                counts = counts.with_disposition(Disposition.SKIPPED)
                continue

            await cur.execute(
                """
                INSERT INTO regulation_article (
                    id, document_id, article_key, seq_in_doc, unit_type,
                    article_no, branch_no, title,
                    text_raw, text_norm, text_norm_sha256, norm_rule_version,
                    amendment_markers, body, heading_path,
                    body_norm, body_norm_sha256, body_markers,
                    reference_raw, moves,
                    article_key_source, valid_from, valid_from_source,
                    known_from, load_run_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    'API', NULL, %s,
                    %s, %s
                )
                """,
                (
                    uuid4(),
                    document_id,
                    payload["article_key"],
                    payload["seq_in_doc"],
                    payload["unit_type"],
                    payload["article_no"],
                    payload["branch_no"],
                    payload["title"],
                    payload["text_raw"],
                    payload["text_norm"],
                    payload["text_norm_sha256"],
                    payload["norm_rule_version"],
                    Jsonb(payload["amendment_markers"]),
                    Jsonb(payload["body"]),
                    payload["heading_path"],
                    payload["body_norm"],
                    payload["body_norm_sha256"],
                    Jsonb(payload["body_markers"]),
                    payload["reference_raw"],
                    Jsonb(payload["moves"]),
                    PENDING_HISTORY,
                    now,
                    load_run_id,
                ),
            )
            counts = counts.with_disposition(loaded_disposition)

    counts.verify_partition(context=f"문서 {document.law_id}/{mst}")
    return DocumentLoadResult(
        document_id=document_id,
        law_id=document.law_id,
        mst=mst,
        document_effective_date=document.document_effective_date,
        counts=counts,
        unresolved_ministry=None if resolution.resolved else document.ministry,
    )


def _reject_document_mismatch(
    existing: dict[str, Any],
    document: LawDocument,
    observation: MinistryObservation,
) -> None:
    """이미 있는 문서와 새 문서의 필드가 다르면 충돌로 다룬다."""
    incoming = {
        "law_name": document.law_name,
        "law_kind": document.law_kind,
        "ministry_code": observation.code_field,
        "ministry_name_observed": document.ministry,
        "promulgation_date": document.promulgation_date,
    }
    differing = {
        field: (existing[field], value)
        for field, value in incoming.items()
        if existing[field] != value
    }
    if differing:
        raise KeyConflictError(
            key=(document.law_id, str(existing["id"])),
            existing={key: existing[key] for key in incoming},
            incoming=incoming,
        )


async def _record_unresolved(
    cur: psycopg.AsyncCursor[Any],
    *,
    resolution: Resolution,
    observation: MinistryObservation,
    document_id: UUID,
    load_run_id: UUID,
    now: dt.datetime,
) -> None:
    """미해결 부처를 append-only 로 기록한다. 관측 횟수는 COUNT(*) 로 센다."""
    assert resolution.reason is not None  # noqa: S101 — resolved 가 아닐 때만 호출된다
    logger.warning(
        "소관부처 미해결: name=%s code=%s reason=%s document_id=%s",
        observation.name_field,
        observation.code_field,
        resolution.reason.value,
        document_id,
    )
    await cur.execute(
        """
        INSERT INTO ministry_unresolved (
            id, observed_name, observed_code, document_id, load_run_id, observed_at, reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(),
            observation.name_field or "",
            observation.code_field,
            document_id,
            load_run_id,
            now,
            resolution.reason.value,
        ),
    )


async def load_snapshot(
    conn: psycopg.AsyncConnection[Any],
    store: DocumentStore,
    manifest: Manifest,
    *,
    now: dt.datetime,
) -> RunResult:
    """매니페스트 하나를 적재하고 마지막에 `load_run` 을 기록한다.

    목적:
        스냅샷 → 파서 → 적재 경로 전체를 한 번 돌린다.

    구현 이유:
        `read_pages` 가 sha256 을 검증하므로 적재 경로는 별도 무결성 검사를 하지
        않는다. 검증 경로를 하나로 유지하는 것이 목적이다 — 두 번째 경로가 생기면
        그것이 다음 사건의 발생지가 된다.

    트레이드오프:
        페이지와 매니페스트의 `PageRecord` 를 순서로 짝짓는다. `read_pages` 가
        매니페스트 순서를 보장하므로 성립하지만, 그 계약이 깨지면 조용히 잘못된
        sha256 이 문서에 기록된다. 페이지 본문을 다시 해싱해 대조하는 대신 계약에
        의존했다 — 같은 계산을 두 번 하지 않기 위해서다.

    엣지 케이스:
        - 페이지 0개 매니페스트: 문서 0건으로 완료된다. 수집 단계에서 이미
          걸러지므로 여기서 실패로 만들지 않는다.
        - 중간 실패: 예외가 그대로 올라가고 `load_run` 은 기록되지 않는다.
          앞서 커밋된 문서는 고아 문서로 남으며 조회로 찾을 수 있다.
    """
    started_at = now
    load_run_id = uuid4()
    master = await fetch_ministry_master(conn)

    counts = LoadCounts()
    results: list[DocumentLoadResult] = []
    mst = mst_of_manifest(manifest)

    index = 0
    async for body in read_pages(store, manifest):
        page = manifest.pages[index]
        index += 1
        document = parse_law_document(body.decode("utf-8"))
        result = await load_document(
            conn,
            document,
            mst=mst,
            load_run_id=load_run_id,
            source_key=manifest.directory,
            source_run_id=manifest.run_id,
            page_sha256=page.sha256,
            master=master,
            now=now,
        )
        results.append(result)
        counts = counts.merge(result.counts)

    counts.verify_partition(context=f"run {manifest.run_id}")

    completed_at = now
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO load_run (
                id, source_key, source_run_id, started_at, completed_at,
                documents_loaded, parsed_units, loaded, loaded_unresolved,
                skipped, key_conflicts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                load_run_id,
                manifest.directory,
                manifest.run_id,
                started_at,
                completed_at,
                len(results),
                counts.parsed_units,
                counts.loaded,
                counts.loaded_unresolved,
                counts.skipped,
                counts.key_conflicts,
            ),
        )
    await conn.commit()

    return RunResult(
        source_key=manifest.directory,
        source_run_id=manifest.run_id,
        started_at=started_at,
        completed_at=completed_at,
        counts=counts,
        documents=tuple(results),
        load_run_id=load_run_id,
    )


async def find_orphan_documents(conn: psycopg.AsyncConnection[Any]) -> tuple[UUID, ...]:
    """어느 완료된 run 에도 속하지 않는 문서를 찾는다.

    목적:
        "문서 단위로는 완전, run 단위로는 미완료"인 상태를 보이게 한다.

    구현 이유:
        문서 단위로 커밋하기로 한 결정의 대가가 고아 문서다. 남는 것을 막을 수
        없다면 최소한 찾을 수 있어야 한다 — 찾을 수 없는 부분 상태는 완전한 것과
        구별되지 않고, 그 구별 불가가 이 저장소가 반복해서 겪은 실패 형태다.

    트레이드오프:
        전체 스캔이다. 문서 수가 늘면 비용이 오르지만 운영 점검 경로이므로
        질의 빈도가 낮다.

    엣지 케이스:
        - 정상 완료된 run 의 문서: `load_run` 에 같은 id 가 있으므로 제외된다.
        - 여러 run 이 실패한 경우: 전부 반환한다. 재적재 판단은 사람이 한다.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT d.id
              FROM regulation_document d
         LEFT JOIN load_run r ON r.id = d.load_run_id
             WHERE r.id IS NULL
             ORDER BY d.known_from
            """
        )
        rows = await cur.fetchall()
    return tuple(row[0] for row in rows)
