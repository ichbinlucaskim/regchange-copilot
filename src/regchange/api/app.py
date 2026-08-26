"""검토 UI 와 승인 엔드포인트 — **판정하지 않고 그래프에 전달한다** (원칙 4).

목적:
    담당자가 검토 대기 목록과 상세를 보고 수락·수정·반려를 보내는 최소 인터페이스.
    승인은 여기서 처리되지 않고 **중단된 그래프를 재개**하는 것으로만 이루어진다.

구현 이유:
    **이 계층은 승인 규칙을 모른다.** 요청을 `Command(resume=...)` 로 바꿔 그래프에
    넘길 뿐이다. API 가 승인 규칙을 알기 시작하면 규칙이 두 곳에 존재하게 되고, 다른
    진입점(배치, 큐 소비자)이 생겼을 때 한쪽만 갱신되어 우회 경로가 열린다.

    **승인 레코드를 API 가 쓰지 않는다.** 여기서 `review_decision` 을 INSERT 하면
    "그래프를 거치지 않고 승인하는 경로"가 생긴다 — 원칙 4 가 막으려는 바로 그것이다.
    그래서 이 모듈은 검토용 커넥션으로 **읽기만** 하고, 쓰기는 그래프의 승인 이후 노드가
    한다. 두 노드만이 `app_review` 커넥션을 보며, 그 노드에 도달하는 유일한 간선이
    `human_review` 다 (`graph/build.py`).

    **화면을 서버가 렌더링한다.** SPA 를 두면 화면과 API 가 따로 움직이고, 검토 화면은
    지금 "무엇을 보여줘야 승인이 형식화되지 않는가"를 실험하는 중이다. 한 파일 안에서
    바꿀 수 있어야 한다.

    **화면이 반드시 보여주는 것 넷**:
      1. `NEEDS_REVIEW` 경고 — 뒷받침되지 않아 제거된 주장이 있었다는 사실
      2. 판정 분포(SUPPORTED/PARTIAL/UNSUPPORTED)와 **각 판정의 이유**
      3. 부서 배정의 근거 조항과 표현, 그리고 **간접 도출 여부**
      4. gate 가 폐기한 것들
    넷 다 "결론만 보고 승인"을 어렵게 만들기 위한 것이다 (F-7).

    **검토 소요 시간을 브라우저가 잰다.** 서버에서 큐 진입 시각과의 차이로 계산하면
    "화면을 열어 둔 채 퇴근한 시간"이 검토 시간이 된다. F-7 감시가 재려는 값은 그것이
    아니다.

트레이드오프:
    - 인증이 없다. 로컬 단일 사용자 운영이며(ADR-014), `decided_by` 를 화면에서 받는다.
      **이것은 축 2(은행 IT 제약)를 만족하지 않는다** — 실제 배포에는 전자결재 연동과
      SSO 가 필요하고, 그것은 이 단계의 범위가 아니다. 지금 가짜 인증을 만들면 나중에
      진짜와 섞인다.
    - HTML 을 문자열로 만든다. 템플릿 엔진을 넣으면 의존성이 늘고, 화면이 두 파일이 된다.
    - 목록과 상세가 같은 질의를 쓴다(`draft_json` 전체를 읽는다). 목록에는 과하지만,
      요약만 읽으면 상세가 다시 질의해야 하고 그 사이에 상태가 바뀔 수 있다.

엣지 케이스:
    - **없는 평가**: 404. 권한 없음과 부재를 뭉뚱그리지 않는다 — 지금은 인증이 없으므로
      구별할 것이 없지만, 생겼을 때 이 구조가 남아 있어야 한다.
    - **이미 결정된 평가**: 409. 두 번째 승인이 발송 대상을 하나 더 만들지 않는다.
    - **큐에 없는 평가**(근거 부족으로 이관): 상세는 볼 수 있고 결정은 409 다. 볼 수 있어야
      하는 이유는 **"모른다"고 판정한 것도 담당자가 확인할 대상**이기 때문이다.
    - **그래프가 중단 상태가 아님**: 재개가 아무 일도 하지 않는다. 그 경우 승인 레코드도
      만들어지지 않으므로 상태는 `PENDING` 으로 남는다 — 조용히 성공으로 보이지 않는다.
"""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from regchange.review.models import DecisionRequest, ReviewItem, ReviewState
from regchange.review.queue import (
    count_overdue,
    list_pending,
    load_item,
    summarize_assessments,
    summarize_reviews,
)
from regchange.store.dsn import DbRole, role_dsn

logger = logging.getLogger(__name__)

type Resumer = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""중단된 그래프를 재개하는 함수 — `(thread_id, 판단) -> 최종 상태`.

주입받는 이유는 이 모듈이 그래프를 조립하지 않기 때문이다. 조립에는 커넥션·모델·임베딩이
필요하고, 그것을 API 가 알면 검토 화면이 파이프라인 구성에 결합된다."""


def create_app(*, resume: Resumer, dsn: str | None = None) -> FastAPI:
    """검토 UI 앱을 만든다. 승인은 `resume` 을 통해서만 이루어진다.

    목적:
        읽기 전용 검토 화면과, 그래프 재개를 트리거하는 엔드포인트를 노출한다.

    구현 이유:
        `resume` 을 주입받는다. 이 함수가 그래프를 만들면 API 가 모델·임베딩·커넥션을
        알게 되고, 테스트가 실제 모델을 부르지 않고는 화면을 못 띄우게 된다.

        커넥션은 `app_review` 로 연다. 이 앱은 읽기만 하지만 **읽는 것도 검토자의 권한으로
        읽어야** 한다 — 화면이 보는 것과 검토자가 볼 수 있는 것이 같아야 하기 때문이다.

    트레이드오프:
        커넥션을 앱 수명 동안 하나 유지한다. 동시 요청이 많으면 병목이지만, 단일 검토자
        운영에서 풀을 도입하면 검증할 것만 는다.

    엣지 케이스:
        모듈 docstring 참조.
    """
    connection: dict[str, psycopg.AsyncConnection[Any]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        conn = await psycopg.AsyncConnection.connect(dsn or role_dsn(DbRole.REVIEW))
        connection["conn"] = conn
        try:
            yield
        finally:
            await conn.close()

    app = FastAPI(title="RegChange Copilot — 검토 큐", lifespan=lifespan)

    def conn() -> psycopg.AsyncConnection[Any]:
        return connection["conn"]

    @app.get("/health")
    async def health() -> dict[str, str]:
        """살아 있는지만 답한다."""
        return {"status": "ok"}

    @app.get("/api/reviews")
    async def api_reviews() -> dict[str, Any]:
        """검토 대기 목록과 기한 집계."""
        items = await list_pending(conn())
        counts = await count_overdue(conn())
        return {
            "pending": counts.pending,
            "overdue": counts.overdue,
            "unknown_due": counts.unknown_due,
            "items": [item.model_dump(mode="json") for item in items],
        }

    @app.get("/api/reviews/{assessment_id}")
    async def api_review(assessment_id: UUID) -> dict[str, Any]:
        """평가 한 건의 전부. 초안·판정·폐기 기록이 함께 나온다."""
        item = await load_item(conn(), assessment_id)
        if item is None:
            raise HTTPException(status_code=404, detail="그런 영향평가가 없다")
        return item.model_dump(mode="json")

    @app.get("/api/metrics")
    async def api_metrics() -> dict[str, Any]:
        """반려율·검토 소요(F-7)와 이관 비율(ADR-013 신호 4번).

        **스모크 실행 산출물은 세지 않는다** (`review/queue.py` 의 `SMOKE_*`).
        """
        return {
            "reviews": await summarize_reviews(conn()),
            "assessments": await summarize_assessments(conn()),
        }

    @app.post("/api/reviews/{assessment_id}/decision")
    async def api_decision(assessment_id: UUID, request: DecisionRequest) -> JSONResponse:
        """판단을 그래프에 전달한다. 이 함수는 승인 레코드를 쓰지 않는다.

        승인 레코드와 발송 대상은 재개된 그래프의 노드가 만든다. 여기서 쓰면 그래프를
        거치지 않는 승인 경로가 생기고, 그것이 원칙 4 가 막으려는 것이다.
        """
        item = await load_item(conn(), assessment_id)
        if item is None:
            raise HTTPException(status_code=404, detail="그런 영향평가가 없다")
        if item.review_state is not ReviewState.PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"이미 {item.review_state.value} 상태다. 두 번째 결정을 받지 않는다",
            )

        final = await resume(item.thread_id, request.model_dump(mode="json"))
        after = await load_item(conn(), assessment_id)
        if after is None or after.review_state is ReviewState.PENDING:
            # 그래프가 중단 상태가 아니었거나 재개가 승인 노드에 닿지 못했다.
            # 조용히 200 을 돌려주면 화면은 승인됐다고 표시하고 실제로는 아무 일도 없다.
            raise HTTPException(
                status_code=409,
                detail=(
                    "그래프가 재개되지 않았거나 승인 노드에 닿지 않았다. "
                    "승인 레코드가 만들어지지 않았으므로 이 결정은 반영되지 않았다"
                ),
            )
        return JSONResponse(
            {
                "review_state": after.review_state.value,
                "decision_id": final.get("decision_id"),
                "outbox_ids": final.get("outbox_ids", []),
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """검토 대기 목록 화면."""
        items = await list_pending(conn())
        counts = await count_overdue(conn())
        return _render_index(items, counts.overdue, counts.unknown_due)

    @app.get("/reviews/{assessment_id}", response_class=HTMLResponse)
    async def detail(assessment_id: UUID) -> str:
        """상세 화면 — 개정 조문, 초안, 인용, 검증 결과, 폐기 기록."""
        item = await load_item(conn(), assessment_id)
        if item is None:
            raise HTTPException(status_code=404, detail="그런 영향평가가 없다")
        return _render_detail(item)

    return app


def _esc(value: object) -> str:
    """HTML 이스케이프. 초안은 모델이 만든 텍스트이므로 반드시 거친다."""
    return html.escape(str(value))


def _render_judgment(judgment: dict[str, Any]) -> str:
    """판정 한 줄. 관계가 있으면 앞세우고 사상된 등급을 괄호로 덧붙인다."""
    relation = judgment.get("relation")
    level = judgment.get("level")
    verdict = f"{_esc(relation)} <small>({_esc(level)})</small>" if relation else _esc(level)
    return (
        f"<li>{_esc(judgment.get('key'))} — <b>{verdict}</b>: {_esc(judgment.get('reason'))}</li>"
    )


def _render_index(items: tuple[ReviewItem, ...], overdue: int, unknown_due: int) -> str:
    """대기 목록 — 기한 초과와 기한 미상을 따로 보여준다."""
    rows = "\n".join(
        f"""<tr>
          <td><a href="/reviews/{_esc(i.id)}">{_esc(i.law_name)} {_esc(i.article_path)}</a></td>
          <td>{_esc(i.status)}</td>
          <td>{_esc(i.risk_level)}</td>
          <td>{_esc(i.due_at.date() if i.due_at else "미상")}</td>
          <td>{_esc(i.queued_at.date() if i.queued_at else "-")}</td>
        </tr>"""
        for i in items
    )
    return f"""{_HEAD}
    <h1>검토 대기 {len(items)}건</h1>
    <p class="warn">기한 초과 {overdue}건 · 기한 미상 {unknown_due}건
      <small>기한은 개정 조문의 시행일이며, 확보하지 못한 건은 초과로 세지 않는다</small></p>
    <table>
      <tr><th>개정</th><th>판정</th><th>위험도</th><th>기한(시행일)</th><th>대기 시작</th></tr>
      {rows or '<tr><td colspan="5">대기 중인 건이 없다</td></tr>'}
    </table>
    </body></html>"""


def _render_detail(item: ReviewItem) -> str:
    """상세 — 결론만 보고 승인하기 어렵도록 폐기·판정·근거를 모두 편다."""
    draft = item.draft
    grounding = item.grounding
    counts = grounding.get("counts", {})
    warn = (
        '<p class="warn">뒷받침되지 않아 <b>제거된 주장이 있다</b>. '
        f"제거: {_esc(grounding.get('unsupported'))}</p>"
        if grounding.get("unsupported")
        else ""
    )
    if item.status == "NEEDS_REVIEW":
        warn += '<p class="warn">상태 <b>NEEDS_REVIEW</b> — 판단이 갈리는 지점이 있다.</p>'
    if item.revisions:
        # 실측 근거가 있는 경고다. 골든셋 case-013 에서 재작성은 **주장을 고치지 않고
        # 근거에 맞게 약화시켰고**, 그 결과 판정이 UNSUPPORTED 에서 SUPPORTED 로 뒤집혔다
        # (`docs/12-impact-assessment-results.md` §5). 코드는 "근거가 늘어서 통과"와
        # "주장이 약해져서 통과"를 가르지 못한다. 사람이 대조해야 한다.
        warn += (
            f'<p class="warn">이 초안은 <b>재작성 {_esc(item.revisions)}회</b>를 거쳤다 — '
            "검증이 한 번 떨어뜨린 뒤 다시 쓴 것이다. 근거가 늘어서 통과한 것인지 "
            "<b>주장이 약해져서</b> 통과한 것인지 코드는 가르지 못한다. "
            "아래 주장과 인용문을 직접 대조하라.</p>"
        )

    impacts = "\n".join(
        f"""<li><b>{_esc(i.get("paragraph_id"))}</b>
          <div class="claim">{_esc(i.get("claim"))}</div>
          <blockquote>{_esc(i.get("quote"))}</blockquote>
          <div class="ctrl">통제항목: {_esc(" / ".join(i.get("control_items") or []))}</div>
        </li>"""
        for i in draft.get("impacts") or []
    )
    affected = {i.get("paragraph_id") for i in draft.get("impacts") or []}
    depts = "\n".join(
        f"""<li><b>{_esc(d.get("department"))}</b> ({_esc(d.get("derivation"))})
          {
            '<span class="indirect">간접 도출 — 근거 조항이 영향 문단이 아니다</span>'
            if d.get("basis_paragraph_id") not in affected
            else ""
        }
          <blockquote>{_esc(d.get("basis_quote"))}</blockquote>
          <div class="claim">{_esc(d.get("rationale"))}</div>
        </li>"""
        for d in draft.get("departments") or []
    )
    # de-anchored 판정에는 관계(WITHIN/BEYOND/UNRELATED)가 함께 온다. 있으면 그것을
    # 앞세운다 — 검토자가 "정도"가 아니라 "무엇을 넘어섰는가"를 봐야 한다.
    judgments = "\n".join(_render_judgment(j) for j in grounding.get("judgments") or [])

    discarded = (
        "\n".join(
            f"<li><b>{_esc(d.get('reason') or d.get('rule'))}</b>"
            f" {_esc(d.get('kind') or '')} {_esc(d.get('label') or d.get('claim') or '')}"
            f" <small>{_esc(d.get('paragraph_id') or d.get('detail') or '')}</small></li>"
            for d in item.discarded
        )
        or "<li>없음</li>"
    )

    return f"""{_HEAD}
    <a href="/">← 목록</a>
    <h1>{_esc(item.law_name)} {_esc(item.article_path)}</h1>
    <p>판정 <b>{_esc(item.status)}</b> · 위험도 {_esc(item.risk_level)} ·
       확신 {_esc(item.confidence)} · 재작성 {_esc(item.revisions)}회 ·
       기한 {_esc(item.due_at.date() if item.due_at else "미상")}</p>
    {warn}
    <h2>요약</h2><p>{_esc(item.summary)}</p>
    <p class="claim">{_esc(item.reason)}</p>

    <h2>영향 문단 {len(draft.get("impacts") or [])}건</h2><ul>{impacts or "<li>없음</li>"}</ul>
    <h2>부서 배정</h2><ul>{depts or "<li>없음</li>"}</ul>

    <h2>gate 3단 판정 {_esc(counts)}</h2><ul>{judgments or "<li>판정 없음</li>"}</ul>
    <h2>gate 가 폐기한 것</h2><ul>{discarded}</ul>

    <h2>결정</h2>
    <form id="f">
      <label>결정
        <select name="decision">
          <option value="ACCEPT">수락</option>
          <option value="EDIT">수정 승인</option>
          <option value="REJECT">반려</option>
        </select></label>
      <label>사유 코드
        <select name="reason_code">
          <option value="">(없음)</option>
          <option value="WRONG_PARAGRAPH">조항을 잘못 골랐다</option>
          <option value="MISSED_PARAGRAPH">걸리는 조항을 놓쳤다</option>
          <option value="WRONG_DEPARTMENT">부서 배정이 틀렸다</option>
          <option value="WRONG_RISK">위험도가 틀렸다</option>
          <option value="NOT_APPLICABLE">우리 영역이 아니다</option>
          <option value="INSUFFICIENT_BASIS">근거가 주장을 뒷받침하지 않는다</option>
          <option value="OTHER">기타 (사유 기술 필수)</option>
        </select></label>
      <label>사유 <textarea name="reason_note" rows="3"></textarea></label>
      <label>검토자 <input name="decided_by" value="reviewer"></label>
      <button type="submit">제출</button>
    </form>
    <p id="out"></p>
    <script>
      // 검토 소요를 브라우저가 잰다. 서버에서 큐 진입 시각과의 차이로 계산하면
      // "화면을 열어 둔 채 퇴근한 시간"이 검토 시간이 된다.
      const opened = Date.now();
      document.getElementById('f').addEventListener('submit', async (e) => {{
        e.preventDefault();
        const f = new FormData(e.target);
        const body = {{
          decision: f.get('decision'),
          decided_by: f.get('decided_by'),
          reason_code: f.get('reason_code') || null,
          reason_note: f.get('reason_note') || null,
          reviewed_ms: Date.now() - opened,
        }};
        const url = location.pathname.replace('/reviews/', '/api/reviews/') + '/decision';
        const r = await fetch(url, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(body),
        }});
        document.getElementById('out').textContent = r.status + ' ' + await r.text();
      }});
    </script>
    </body></html>"""


_HEAD = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>검토 큐</title><style>
body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}
body{line-height:1.6}
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}
blockquote{border-left:3px solid #ccc;margin:.3rem 0;padding:.2rem .8rem}
blockquote{color:#333;background:#fafafa}
.warn{background:#fff4e5;border-left:4px solid #e07b00;padding:.6rem .8rem}
.claim{color:#444}.ctrl{color:#666;font-size:.9rem}
.indirect{color:#b35c00;font-size:.85rem}
li{margin-bottom:.8rem}label{display:block;margin:.4rem 0}
textarea,input,select{width:100%;max-width:40rem}
</style></head><body>"""
