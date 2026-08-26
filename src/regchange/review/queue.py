"""검토 큐의 질의와 쓰기 — 적재 · 대기 목록 · 승인 기록 · 발송 대상 생성.

목적:
    영향평가를 큐에 넣고, 대기 목록과 기한 초과를 읽고, 사람의 판단을 승인 레코드로
    남기며, 승인된 건에서만 발송 대상을 만든다.

구현 이유:
    **적재와 승인이 서로 다른 커넥션을 쓴다.** `insert_assessment` 는 `app_graph` 로,
    `record_decision` 은 `app_review` 로 부른다. 이 함수들은 커넥션을 만들지 않고 받으므로
    **어느 role 로 붙을지는 호출부가 정한다** — 여기서 만들면 이 모듈이 role 을 고르게 되고,
    그러면 원칙 5 의 경계가 모듈 하나의 판단에 걸린다.

    권한이 실제 방어선이다. `app_graph` 로 `record_decision` 을 부르면 DB 가 거부한다.
    이 모듈이 검사하는 것이 아니라 **DB 가 거부한다** — 코드로 강제한 경계는 코드 수정으로
    무너지지만 권한으로 강제한 경계는 그렇지 않다.

    **승인과 발송 대상 생성이 한 트랜잭션이다.** 나누면 "승인은 됐는데 발송 대상이 없는"
    상태가 생기고, 그 상태는 아무 오류도 내지 않으면서 담당자의 승인을 무효로 만든다.

    **재승인을 막는다.** `review_state` 가 `PENDING` 이 아니면 거부한다. 두 번째 승인이
    outbox 행을 하나 더 만들면 발송이 두 번 일어난다 — API 멱등성 요구(`api/__init__.py`
    엣지 케이스)를 DB 수준에서 지키는 자리다.

트레이드오프:
    - `UPDATE ... WHERE review_state = 'PENDING'` 의 갱신 행 수로 재승인을 판정한다.
      별도 SELECT 로 확인하면 그 사이에 다른 요청이 끼어들 수 있다. 조건부 UPDATE 는
      원자적이지만 "왜 실패했는가"가 행 수 0 이라는 사실뿐이어서, 사유를 다시 질의한다.
    - 발송 대상의 `payload` 를 여기서 만든다. 승인된 초안에서 **발송에 필요한 것만** 옮기며,
      프롬프트도 모델 출력 원본도 담지 않는다 (원칙 5). 무엇이 필요한지는 발송 채널이
      정해지면 바뀔 수 있고, 그때 이 함수가 바뀐다.

엣지 케이스:
    - **이미 결정된 건**: `ReviewError`. 조용히 두 번째 승인 레코드를 만들지 않는다.
    - **큐에 없는 건에 대한 결정**: `ReviewError`. `NOT_QUEUED` 는 사람이 처리할 대상이
      아니다.
    - **반려**: 승인 레코드는 남기고 발송 대상은 만들지 않는다. 반려도 결정이며 이력이다.
    - **기한이 없는 대기**: `count_overdue` 가 기한 초과로 세지 않고 `unknown_due` 로 따로
      센다.
    - **`reviewed_ms` 가 음수**: DB CHECK 가 거부한다. 시계 문제를 0 으로 덮지 않는다.
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

from regchange.review.models import (
    DecisionRequest,
    ReviewDecisionKind,
    ReviewItem,
    ReviewState,
)

logger = logging.getLogger(__name__)

ACTION_TYPE_POLICY_REVISION = "POLICY_REVISION_DRAFT"
"""발송 대상의 종류. 지금은 하나뿐이다 — 승인된 영향평가에서 기안 문서를 만드는 작업.

값을 문자열 상수로 두는 이유는 발송 워커가 이 값으로 분기하기 때문이며, 워커는 이
저장소의 다른 코드를 보지 않는다 (원칙 5)."""


SMOKE_REVIEWERS = frozenset({"smoke-test"})
"""통계에서 제외하는 검토자 이름.

**지우지 않고 세지 않는다.** 4단계에서 그래프 전 구간을 실제 모델로 한 번 돌렸고
(`docs/12-impact-assessment-results.md` §11), 그 실행이 운영 DB 에 승인 레코드 1행과
발송 대상 1행을 남겼다. 세 테이블 모두 불변이라 지울 수 없고, 지우려면 소유자 `TRUNCATE`
가 필요한데 그것은 이 저장소가 사건으로 기록한 행위다
(`docs/incidents/test-truncated-operations-history.md`).

**남겨 두고 세지 않는다.** 반려율과 검토 소요는 사람의 판단을 재는 값이며, 스모크 실행이
섞이면 첫 실적이 30초짜리 자동 승인 1건으로 시작한다."""

SMOKE_ASSESSMENT_IDS = frozenset(
    {
        UUID("ba2ec419-1e81-48cf-baac-58945e98d7d7"),  # case-009, 근거 부족 경로 확인
        UUID("9a591a63-610d-4fcb-a1cf-25da87640b6c"),  # case-008, 승인 경로 확인
    }
)
"""통계에서 제외하는 평가 id.

**왜 이름이 아니라 id 목록인가**: 근거 부족으로 이관된 평가에는 승인 레코드가 없어
`decided_by` 로 걸러지지 않는다. 그리고 `impact_assessment` 는 `review_state` 외의 컬럼을
UPDATE 할 수 없으므로(011 트리거) **표시 컬럼을 나중에 붙여 소급 적용할 수 없다.**

앞으로 같은 일이 반복되지 않게 하려면 `origin`(OPERATIONAL / SMOKE) 컬럼을 넣고 적재
시점에 채워야 한다. 지금 넣으면 이 두 행이 기본값 `OPERATIONAL` 로 잘못 표시되므로,
**두 행에 한해 명시 목록으로 두고** 컬럼은 다음 스키마 변경에 함께 넣는다."""


class ReviewError(RuntimeError):
    """검토 큐의 상태 때문에 요청을 수행할 수 없다. 조용히 넘기지 않는다."""


@dataclass(frozen=True, slots=True)
class OverdueCount:
    """기한 관련 집계. 기한 초과와 기한 미상을 구별해서 센다."""

    pending: int
    overdue: int
    unknown_due: int
    """기한(개정 시행일)을 확보하지 못한 대기 건수. 기한 초과와 다른 사실이다."""
    oldest_pending_days: int | None


_INSERT_ASSESSMENT = """
INSERT INTO impact_assessment (
    id, thread_id, created_at,
    law_name, article_path, revision_kind, change_type, as_of,
    status, obligation_type, risk_level, confidence, summary, reason, revisions,
    draft_json, grounding_json, discarded_json,
    queued_at, due_at, review_state
) VALUES (
    %(id)s, %(thread_id)s, %(created_at)s,
    %(law_name)s, %(article_path)s, %(revision_kind)s, %(change_type)s, %(as_of)s,
    %(status)s, %(obligation_type)s, %(risk_level)s, %(confidence)s, %(summary)s,
    %(reason)s, %(revisions)s,
    %(draft)s, %(grounding)s, %(discarded)s,
    %(queued_at)s, %(due_at)s, %(review_state)s
)
"""

_SELECT_COLUMNS = """
    id, thread_id, created_at, law_name, article_path, revision_kind, change_type,
    status, review_state, obligation_type, risk_level, confidence, summary, reason,
    revisions, draft_json, grounding_json, discarded_json, queued_at, due_at
"""


async def insert_assessment(
    conn: psycopg.AsyncConnection[Any],
    *,
    assessment_id: UUID,
    thread_id: str,
    law_name: str,
    article_path: str,
    revision_kind: str,
    change_type: str,
    as_of: dt.date,
    status: str,
    obligation_type: str,
    risk_level: str,
    confidence: str,
    summary: str,
    reason: str,
    revisions: int,
    draft: dict[str, Any],
    grounding: dict[str, Any],
    discarded: list[dict[str, Any]],
    queued: bool,
    due_at: dt.datetime | None,
    created_at: dt.datetime | None = None,
) -> UUID:
    """영향평가 한 건을 적재한다. 큐에 넣을지 여부를 호출부가 정한다.

    목적:
        검토 화면이 질의할 수 있는 형태로 초안을 남긴다.

    구현 이유:
        `queued` 를 인자로 받는다. "근거가 부족하니 큐에 넣지 않는다"는 판정은 gate 의
        결과이며 이 함수가 다시 판단할 것이 아니다. 다만 **행은 반드시 남긴다** — 이관된
        건이 몇 %인지(ADR-013 신호 4번)를 세려면 그 행이 있어야 한다.

    트레이드오프:
        커밋하지 않는다. 호출부의 트랜잭션 경계 안에서 돌기 위해서다.

    엣지 케이스:
        - `queued=False` 인데 `due_at` 이 주어짐: DB CHECK 가 거부한다. 큐에 없는 건에
          기한은 의미가 없다.
    """
    stamp = created_at or dt.datetime.now(dt.UTC)
    async with conn.cursor() as cursor:
        await cursor.execute(
            _INSERT_ASSESSMENT,
            {
                "id": assessment_id,
                "thread_id": thread_id,
                "created_at": stamp,
                "law_name": law_name,
                "article_path": article_path,
                "revision_kind": revision_kind,
                "change_type": change_type,
                "as_of": as_of,
                "status": status,
                "obligation_type": obligation_type,
                "risk_level": risk_level,
                "confidence": confidence,
                "summary": summary,
                "reason": reason,
                "revisions": revisions,
                "draft": Jsonb(draft),
                "grounding": Jsonb(grounding),
                "discarded": Jsonb(discarded),
                "queued_at": stamp if queued else None,
                "due_at": due_at if queued else None,
                "review_state": (
                    ReviewState.PENDING.value if queued else ReviewState.NOT_QUEUED.value
                ),
            },
        )
    logger.info(
        "영향평가 적재: id=%s status=%s queued=%s due=%s",
        assessment_id,
        status,
        queued,
        due_at.date() if due_at else "미상",
    )
    return assessment_id


def _to_item(row: dict[str, Any]) -> ReviewItem:
    """행 하나를 검토 항목으로 바꾼다."""
    return ReviewItem(
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        created_at=row["created_at"],
        law_name=str(row["law_name"]),
        article_path=str(row["article_path"]),
        revision_kind=str(row["revision_kind"]),
        change_type=str(row["change_type"]),
        status=str(row["status"]),
        review_state=ReviewState(str(row["review_state"])),
        obligation_type=str(row["obligation_type"]),
        risk_level=str(row["risk_level"]),
        confidence=str(row["confidence"]),
        summary=str(row["summary"]),
        reason=str(row["reason"]),
        revisions=int(row["revisions"]),
        draft=dict(row["draft_json"] or {}),
        grounding=dict(row["grounding_json"] or {}),
        discarded=list(row["discarded_json"] or []),
        queued_at=row["queued_at"],
        due_at=row["due_at"],
    )


async def list_pending(
    conn: psycopg.AsyncConnection[Any], *, limit: int = 50
) -> tuple[ReviewItem, ...]:
    """검토 대기 목록. **기한이 이른 것부터**, 기한 미상은 뒤에 둔다.

    기한 미상을 앞에 두면 기한이 임박한 건이 밀린다. 뒤에 두되 `ops alerts` 가 그 수를
    따로 보고하므로 잊히지 않는다.
    """
    query = f"""
        SELECT {_SELECT_COLUMNS}
          FROM impact_assessment
         WHERE queued_at IS NOT NULL
           AND review_state = 'PENDING'
         ORDER BY due_at ASC NULLS LAST, queued_at ASC
         LIMIT %(limit)s
    """  # noqa: S608 — 보간되는 것은 모듈 상수인 컬럼 목록뿐이다 (store/queries.py 와 동일)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, {"limit": limit})
        return tuple(_to_item(row) for row in await cursor.fetchall())


async def load_item(conn: psycopg.AsyncConnection[Any], assessment_id: UUID) -> ReviewItem | None:
    """평가 한 건. 없으면 None — 호출부가 404 를 만든다."""
    query = f"SELECT {_SELECT_COLUMNS} FROM impact_assessment WHERE id = %(id)s"  # noqa: S608
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, {"id": assessment_id})
        row = await cursor.fetchone()
    return _to_item(row) if row else None


async def count_overdue(
    conn: psycopg.AsyncConnection[Any], *, now: dt.datetime | None = None
) -> OverdueCount:
    """대기·기한 초과·기한 미상을 함께 센다. `ops alerts` 가 이 값을 쓴다.

    **기한 초과와 기한 미상을 한 숫자로 합치지 않는다.** 전자는 담당자를 재촉할 사실이고
    후자는 수집 경로를 고칠 사실이다 — 조치가 다르면 지표도 달라야 한다.
    """
    stamp = now or dt.datetime.now(dt.UTC)
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT count(*) AS pending,
                   count(*) FILTER (WHERE due_at IS NOT NULL AND due_at < %(now)s) AS overdue,
                   count(*) FILTER (WHERE due_at IS NULL) AS unknown_due,
                   max(EXTRACT(DAY FROM %(now)s - queued_at))::int AS oldest_days
              FROM impact_assessment
             WHERE queued_at IS NOT NULL
               AND review_state = 'PENDING'
            """,
            {"now": stamp},
        )
        row = await cursor.fetchone()
    assert row is not None  # noqa: S101 — 집계 질의는 항상 한 행이다
    return OverdueCount(
        pending=int(row["pending"]),
        overdue=int(row["overdue"]),
        unknown_due=int(row["unknown_due"]),
        oldest_pending_days=(None if row["oldest_days"] is None else int(row["oldest_days"])),
    )


async def summarize_reviews(conn: psycopg.AsyncConnection[Any]) -> dict[str, Any]:
    """반려율과 검토 소요 분포. F-7(승인 게이트 형식화) 감시 지표다.

    승인이 형식화되면 반려율이 0 에 가까워지고 검토 소요가 짧아진다. 둘을 함께 보지
    않으면 "빠르게 잘 처리되고 있다"와 구별할 수 없다.
    """
    excluded = list(SMOKE_REVIEWERS)
    async with conn.cursor(row_factory=dict_row) as cursor:
        # 스모크 실행의 결정을 세지 않는다 (`SMOKE_REVIEWERS` 참조). 지우지 않고 제외한다.
        await cursor.execute(
            """
            SELECT decision,
                   count(*) AS n,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY reviewed_ms) AS median_ms,
                   min(reviewed_ms) AS min_ms,
                   max(reviewed_ms) AS max_ms
              FROM review_decision
             WHERE decided_by <> ALL(%(excluded)s)
             GROUP BY decision
             ORDER BY decision
            """,
            {"excluded": excluded},
        )
        by_decision = [dict(row) for row in await cursor.fetchall()]
        await cursor.execute(
            """
            SELECT reason_code, count(*) AS n
              FROM review_decision
             WHERE reason_code IS NOT NULL
               AND decided_by <> ALL(%(excluded)s)
             GROUP BY reason_code
             ORDER BY n DESC
            """,
            {"excluded": excluded},
        )
        by_reason = [dict(row) for row in await cursor.fetchall()]

    total = sum(int(row["n"]) for row in by_decision)
    rejected = sum(int(row["n"]) for row in by_decision if row["decision"] == "REJECT")
    return {
        "decisions": by_decision,
        "reasons": by_reason,
        "total": total,
        "reject_rate": (rejected / total) if total else None,
        "excluded_reviewers": sorted(SMOKE_REVIEWERS),
    }


async def summarize_assessments(conn: psycopg.AsyncConnection[Any]) -> dict[str, Any]:
    """평가 단위 집계 — **이관 비율**이 ADR-013 의 「신호 4번」이다.

    목적:
        gate 가 통과시키기만 하는지(이관 0%)와 전부 이관하는지(무용)를 한 값으로 본다.

    구현 이유:
        러너가 아니라 DB 에서 센다. 러너 집계는 그 실행만 보고, 운영은 누적을 봐야 한다.
        **스모크 실행 산출물은 제외한다** (`SMOKE_ASSESSMENT_IDS`).

    트레이드오프:
        제외 목록이 코드 상수다. 스키마에 표시 컬럼을 두는 것이 일반적인 해법이지만,
        기존 두 행에 소급 적용할 수 없다 (011 의 불변 트리거). 그 사정을 상수 docstring 에
        적어 두고 컬럼은 다음 스키마 변경에 넣는다.

    엣지 케이스:
        - 평가가 0건: 비율이 `None` 이다. 0.0 으로 두면 "이관이 없었다"와 "잴 것이
          없었다"가 같은 값이 된다.
    """
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT status, count(*) AS n
              FROM impact_assessment
             WHERE id <> ALL(%(excluded)s::uuid[])
             GROUP BY status
             ORDER BY status
            """,
            {"excluded": [str(i) for i in SMOKE_ASSESSMENT_IDS]},
        )
        by_status = [dict(row) for row in await cursor.fetchall()]

    total = sum(int(row["n"]) for row in by_status)
    transferred = sum(
        int(row["n"]) for row in by_status if row["status"] == "INSUFFICIENT_EVIDENCE"
    )
    return {
        "by_status": by_status,
        "total": total,
        "transfer_rate": (transferred / total) if total else None,
        "excluded_assessments": len(SMOKE_ASSESSMENT_IDS),
    }


async def record_decision(
    conn: psycopg.AsyncConnection[Any],
    *,
    assessment_id: UUID,
    request: DecisionRequest,
    decided_at: dt.datetime | None = None,
) -> tuple[UUID, tuple[UUID, ...]]:
    """사람의 판단을 승인 레코드로 남기고, 승인이면 발송 대상을 만든다.

    목적:
        원칙 4 의 마지막 지점. **여기서 만들어진 행만이 발송의 근거다.**

    구현 이유:
        상태 전이와 승인 레코드 삽입을 한 트랜잭션에 둔다. 조건부 UPDATE 로 재승인을 막고,
        갱신 행이 0 이면 왜인지 다시 질의해 구체적인 예외를 만든다 — "실패했다"만으로는
        담당자가 무엇을 해야 할지 알 수 없다.

        발송 대상은 `ACCEPT`/`EDIT` 에서만 만든다. 반려도 결정이므로 승인 레코드는 남지만
        outbox 행은 생기지 않는다.

    트레이드오프:
        `payload` 에 담을 것을 이 함수가 정한다. 발송 채널이 정해지면 바뀔 값이지만,
        지금 정해 두지 않으면 워커가 승인 레코드를 뒤져 모델 출력을 읽게 된다 — 그것이
        원칙 5 가 금지하는 방향이다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    stamp = decided_at or dt.datetime.now(dt.UTC)
    next_state = {
        ReviewDecisionKind.ACCEPT: ReviewState.ACCEPTED,
        ReviewDecisionKind.EDIT: ReviewState.EDITED,
        ReviewDecisionKind.REJECT: ReviewState.REJECTED,
    }[request.decision]

    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE impact_assessment
                   SET review_state = %(state)s
                 WHERE id = %(id)s
                   AND review_state = 'PENDING'
                   AND queued_at IS NOT NULL
             RETURNING id, draft_json, law_name, article_path
                """,
                {"state": next_state.value, "id": assessment_id},
            )
            updated = await cursor.fetchone()

        if updated is None:
            raise ReviewError(await _explain_rejection(conn, assessment_id))

        decision_id = uuid4()
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO review_decision (
                    id, impact_assessment_id, decided_by, decision, decided_at,
                    reason_code, reason_note, edit_json, reviewed_ms
                ) VALUES (
                    %(id)s, %(assessment)s, %(by)s, %(decision)s, %(at)s,
                    %(code)s, %(note)s, %(edit)s, %(ms)s
                )
                """,
                {
                    "id": decision_id,
                    "assessment": assessment_id,
                    "by": request.decided_by,
                    "decision": request.decision.value,
                    "at": stamp,
                    "code": request.reason_code.value if request.reason_code else None,
                    "note": request.reason_note,
                    "edit": Jsonb(request.edit) if request.edit is not None else None,
                    "ms": request.reviewed_ms,
                },
            )

        outbox_ids: list[UUID] = []
        if request.decision is not ReviewDecisionKind.REJECT:
            outbox_id = uuid4()
            draft = dict(updated["draft_json"] or {})
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO action_outbox (
                        id, review_decision_id, action_type, payload, state, created_at
                    ) VALUES (%(id)s, %(decision)s, %(type)s, %(payload)s, 'PENDING', %(at)s)
                    """,
                    {
                        "id": outbox_id,
                        "decision": decision_id,
                        "type": ACTION_TYPE_POLICY_REVISION,
                        "payload": Jsonb(_outbox_payload(updated, draft, request)),
                        "at": stamp,
                    },
                )
            outbox_ids.append(outbox_id)

    logger.info(
        "검토 결정: assessment=%s decision=%s by=%s outbox=%d",
        assessment_id,
        request.decision.value,
        request.decided_by,
        len(outbox_ids),
    )
    return decision_id, tuple(outbox_ids)


def _outbox_payload(
    row: dict[str, Any], draft: dict[str, Any], request: DecisionRequest
) -> dict[str, Any]:
    """발송 워커가 보는 전부. **프롬프트도 모델 출력 원본도 담지 않는다** (원칙 5).

    담는 것은 "무엇에 대해 무엇을 하라"이며, 그 값들은 이미 사람이 승인한 것이다.
    수정 승인이면 수정 내용을 함께 담는다 — 워커가 초안이 아니라 **승인된 내용**을 봐야 한다.
    """
    return {
        "law_name": row["law_name"],
        "article_path": row["article_path"],
        "affected_paragraph_ids": [item.get("paragraph_id") for item in draft.get("impacts") or []],
        "departments": [entry.get("department") for entry in draft.get("departments") or []],
        "risk_level": draft.get("risk_level"),
        "required_evidence": draft.get("required_evidence") or [],
        "decision": request.decision.value,
        "edit": request.edit,
    }


async def _explain_rejection(conn: psycopg.AsyncConnection[Any], assessment_id: UUID) -> str:
    """조건부 UPDATE 가 0행이었을 때 왜인지 알아낸다. "실패"만으로는 조치할 수 없다."""
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT review_state, queued_at FROM impact_assessment WHERE id = %(id)s",
            {"id": assessment_id},
        )
        row = await cursor.fetchone()
    if row is None:
        return f"영향평가 {assessment_id} 가 없다"
    if row["queued_at"] is None:
        return (
            f"영향평가 {assessment_id} 는 검토 큐에 없다 (근거 부족으로 이관된 건이다). "
            "사람이 처리할 대상이 아니다"
        )
    return (
        f"영향평가 {assessment_id} 는 이미 {row['review_state']} 상태다. "
        "두 번째 결정을 기록하지 않는다 — 발송이 두 번 일어난다"
    )
