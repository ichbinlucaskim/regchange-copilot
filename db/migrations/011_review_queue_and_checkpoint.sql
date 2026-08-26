-- ---------------------------------------------------------------------------
-- 011. 영향평가 · 검토 큐 · 승인 레코드 · LangGraph 체크포인트 스키마
-- ---------------------------------------------------------------------------
--
-- 002 는 `impact_assessment` / `review_decision` / `action_outbox` 를 "경계 검증용 최소
-- 정의"로 두고 **"컬럼은 작업 5(승인)에서 확정한다"** 고 적었다. 지금이 그때다.
--
-- 목적:
--   (a) 영향평가 초안을 감사 가능한 형태로 남기고, (b) 사람이 승인·반려한 사실과 그
--   사유를 구조화해 남기며, (c) 승인 없이는 발송 대상이 만들어지지 않는 경계를
--   **DB 권한으로** 강제한다. 그리고 (d) LangGraph 체크포인트를 도메인 스키마 밖에 둔다.
--
-- 구현 이유:
--
--   1. **`app_graph` 에 `impact_assessment` INSERT 를 준다. 경계를 넓히는 것이므로 근거를
--      적는다.**
--
--      원칙 5 는 "LLM 이 관여하는 프로세스는 읽기 전용 role 을 쓴다"이고, 002·003 은 그
--      예외를 `llm_invocation` 과 `audit_event` 둘로 제한했다. 여기서 셋째가 생긴다.
--
--      막으려는 것이 무엇인지로 판단한다. 원칙 5 가 막는 것은 **인젝션이 성립했을 때의
--      피해**이며, 그 피해는 "정책이 바뀐다 / 티켓이 나간다 / 외부로 발송된다"이다.
--      초안 저장은 그중 어느 것도 아니다 — 초안은 **모델 출력이라고 표시된 채** 저장되고,
--      그것만으로는 아무 일도 일어나지 않는다. 무엇이 일어나려면 `review_decision` 이
--      있어야 하고, **`app_graph` 는 그 테이블에 INSERT 할 수 없다.**
--
--      즉 경계는 "쓰기 금지"가 아니라 **"승인 레코드와 발송 대상에 대한 쓰기 금지"** 다.
--      이 마이그레이션은 그 경계를 더 명확하게 만든다:
--
--        | role | impact_assessment | review_decision | action_outbox |
--        |---|---|---|---|
--        | app_graph  | INSERT | **없음** | **없음** |
--        | app_review | UPDATE (review_state) | INSERT | INSERT |
--        | app_dispatch | 없음 (SELECT 도) | 없음 | SELECT, UPDATE |
--
--      초안을 어디에도 저장하지 않는 대안이 있다 — 체크포인트에만 두는 것이다. 그러면
--      검토 큐를 질의할 수 없고(체크포인트는 직렬화된 상태 덩어리다), 검토 대기 목록과
--      기한 초과 판정을 만들 수 없다. 질의할 수 없는 큐는 운영할 수 없다 (축 3).
--
--   2. **`status` 와 `review_state` 를 나눈다. 검토자는 `status` 를 고칠 수 없다.**
--
--      003 은 `GRANT UPDATE (status) ON impact_assessment TO app_review` 였다. 그때
--      `status` 는 한 컬럼이었고 지금은 둘로 갈린다.
--
--        - `status` — gate 가 정한 기계의 판정(OK / NEEDS_REVIEW / INSUFFICIENT_EVIDENCE).
--          **누구도 UPDATE 할 수 없다.** 검토자가 이 값을 바꿀 수 있으면 "시스템이 근거
--          부족이라고 했는데 사람이 충분으로 고쳤다"가 기록에서 사라진다.
--        - `review_state` — 사람의 처리 상태(PENDING / ACCEPTED / EDITED / REJECTED).
--          `app_review` 만 UPDATE 한다.
--
--      003 의 권한보다 **좁아졌다.** 좁히는 방향의 변경이므로 기존 테스트가 깨지면
--      테스트가 옛 경계를 검사하고 있는 것이다.
--
--   3. **초안 본문은 사후에 바뀔 수 없다** (원칙 6). `review_state` 외의 컬럼을 UPDATE
--      하려 하면 트리거가 거부한다. 검토자가 초안을 "수정 승인"하는 경우에도 초안을
--      고치지 않고 **수정 내용을 `review_decision.edit_json` 에 남긴다.** 원본과 수정본이
--      둘 다 남아야 "담당자가 무엇을 고쳤는가"가 6단계의 평가 데이터가 된다.
--
--   4. **반려 사유를 닫힌 코드로 받는다.** 자유 텍스트만 받으면 집계가 불가능하고,
--      집계할 수 없는 반려율은 게이트가 작동하는지 알려주지 않는다 (F-7). 코드 집합은
--      **이 시스템이 틀릴 수 있는 방식**에서 도출했다 — 조항을 잘못 골랐거나(F-6),
--      놓쳤거나(F-1), 부서·위험도를 잘못 정했거나, 우리 영역이 아니거나, 근거가 주장을
--      뒷받침하지 않는 경우다. `OTHER` 는 자유 기술을 강제하며, **`OTHER` 가 쌓이는 것이
--      코드 집합을 늘릴 신호다.**
--
--   5. **기한(`due_at`)을 우리가 정하지 않는다. 개정 조문의 시행일이 기한이다.**
--
--      "검토는 N일 안에"라는 상수를 만들 근거가 이 저장소에 없다. 반면 **실제 기한은
--      데이터에 이미 있다** — 개정 법령의 시행일까지 사내 규정이 정비되어 있어야 하고,
--      그 날짜는 법제처가 준다(`효력발생일`). 우리가 만든 숫자가 아니라 밖에서 온 사실이다.
--
--      시행일을 확보하지 못한 건은 `due_at` 을 NULL 로 두고 큐에는 넣는다. **"기한을 모르는
--      대기"와 "기한이 넉넉한 대기"는 다른 사실**이며, `ops alerts` 가 전자를 따로 센다.
--      임의의 기본 기한으로 채우면 그 구별이 사라지고, 근거 없는 숫자가 운영 지표가 된다.
--
--   6. **검토 소요 시간을 행에 저장한다** (`reviewed_ms`). 두 시각의 차이로 계산할 수도
--      있지만, 저장해 두면 큐 진입 시각의 정의가 바뀌어도 과거 측정이 살아남는다.
--      F-7(승인 게이트가 형식화된다)을 감시하려면 **소요 시간의 분포**를 봐야 하고,
--      분포는 나중에 정의를 바꾸면 비교할 수 없게 된다.
--
--   7. **체크포인트를 별도 스키마에 두고 `app_graph` 가 소유한다** (기획서 13.4).
--      체크포인터는 자기 테이블을 만들고 갱신·삭제한다. 그 권한을 `public` 스키마에서
--      주면 도메인 테이블에 대한 권한과 구별되지 않는다. 스키마를 나누면 "그래프의 작업
--      상태"와 "도메인 데이터"가 권한 수준에서 갈린다.
--
--      **`app_dispatch` 에는 이 스키마에 USAGE 조차 주지 않는다.** 체크포인트에는 프롬프트와
--      모델 출력이 들어 있고, 발송 워커는 그것을 보지 않는다는 것이 원칙 5 다.
--
--   8. **`llm_invocation` 에 `revision` 과 `impact_assessment_id` 를 더한다.**
--      `attempt`(스키마 위반 재시도)와 `revision`(검증 실패로 인한 재작성)은 원인도 조치도
--      다르다. 한 컬럼에 섞으면 ADR-013 의 「신호 2번」(재작성 비율)이 재시도율과 뒤섞인다.
--      `impact_assessment_id` 는 초안 호출과 검증 호출을 한 건으로 묶는다 — **검증 행이
--      없는 초안은 검증받지 않은 초안이며, 그 사실이 질의로 드러나야 한다** (원칙 3).
--
-- 트레이드오프:
--   - `draft_json` 을 jsonb 로 통째로 담는다. 정규화하면 질의는 좋아지지만 초안 스키마가
--     바뀔 때마다 마이그레이션이 필요하고, 4단계에서 그 스키마는 아직 움직인다. 대신
--     집계에 쓰는 값(위험도·의무유형·상태)만 컬럼으로 꺼내 둔다.
--   - `review_state` 를 UPDATE 로 둔다. 이력만으로 현재 상태를 계산할 수도 있지만
--     (`review_decision` 이 이미 이력이다), 검토 큐 질의가 매번 집계를 돌게 된다.
--     **이력이 정본이고 이 컬럼은 파생**이며, 트리거가 다른 컬럼의 변경을 막는다.
--   - 결재선(`approval_line`, 기획서 14.4)을 만들지 않는다. 지금은 단일 승인자다.
--     확장 경로는 ADR-018 에 적었다 — `review_decision` 이 이미 여러 행을 허용하므로
--     다단계는 행이 늘어나는 형태가 되고, 스키마 변경 없이 순서 컬럼 하나를 더하면 된다.
--
-- 엣지 케이스:
--   - **`INSUFFICIENT_EVIDENCE` 평가**: 검토 큐에 넣지 않는다(`queued_at` NULL). 사람이
--     볼 것이 없기 때문이 아니라, **볼 것이 없다는 사실 자체가 결과**이기 때문이다.
--     행은 남으므로 "이관된 건이 몇 %인가"(ADR-013 신호 4번)를 셀 수 있다.
--   - **같은 개정을 두 번 평가**: 두 행이 된다. 멱등을 만들지 않는다 — 재실행은 실제로
--     일어난 사건이고, 어느 초안을 사람이 봤는지는 `thread_id` 와 시각이 가른다.
--   - **승인 없이 outbox 행이 생김**: FK 가 `review_decision` 을 요구하므로 불가능하다.
--     권한과 FK 두 겹이다 — 권한은 role 을 잘못 주면 무너지고 FK 는 그렇지 않다.
--   - **검토자가 초안을 고치려 함**: 트리거가 거부한다. 수정은 `edit_json` 으로 남는다.
--   - **`reviewed_ms` 가 음수**: CHECK 가 거부한다. 큐 진입보다 이른 결정은 시계 문제이며
--     조용히 0 으로 만들면 소요 시간 분포가 왜곡된다.

-- ---------------------------------------------------------------------------
-- llm_invocation 확장 — 재작성과 평가 묶음
-- ---------------------------------------------------------------------------
ALTER TABLE llm_invocation
    ADD COLUMN IF NOT EXISTS revision int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS impact_assessment_id uuid;

ALTER TABLE llm_invocation
    DROP CONSTRAINT IF EXISTS llm_invocation_revision_nonnegative;
ALTER TABLE llm_invocation
    ADD CONSTRAINT llm_invocation_revision_nonnegative CHECK (revision >= 0);

COMMENT ON COLUMN llm_invocation.revision IS
    'evaluator-optimizer 재작성 회차. attempt(스키마 재시도)와 다른 값이다 (ADR-013 신호 2)';
COMMENT ON COLUMN llm_invocation.impact_assessment_id IS
    '초안·검증·재작성 호출을 한 평가로 묶는 키. 검증 행이 없는 초안은 검증받지 않은 초안이다';

CREATE INDEX IF NOT EXISTS llm_invocation_assessment
    ON llm_invocation (impact_assessment_id, purpose, revision);

-- ---------------------------------------------------------------------------
-- 002 의 최소 정의를 대체한다. 참조 순서 때문에 역순으로 지운다.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS action_outbox;
DROP TABLE IF EXISTS review_decision;
DROP TABLE IF EXISTS impact_assessment;

-- ---------------------------------------------------------------------------
-- impact_assessment — 영향평가 한 건. 초안 본문은 불변이다
-- ---------------------------------------------------------------------------
CREATE TABLE impact_assessment (
    id                 uuid PRIMARY KEY,

    -- LangGraph 스레드. 검토 화면의 재개(Command(resume=...))가 이 값을 쓴다.
    thread_id          text        NOT NULL,
    created_at         timestamptz NOT NULL,

    -- 무엇에 대한 평가인가. 개정 조문의 식별 정보를 꺼내 둔다.
    law_name           text        NOT NULL,
    article_path       text        NOT NULL,
    revision_kind      text        NOT NULL,
    change_type        text        NOT NULL,
    as_of              date        NOT NULL,

    -- 기계의 판정. **누구도 UPDATE 할 수 없다** (구현 이유 2).
    status             text        NOT NULL,
    obligation_type    text        NOT NULL,
    risk_level         text        NOT NULL,
    confidence         text        NOT NULL,
    summary            text        NOT NULL,
    reason             text        NOT NULL,
    revisions          int         NOT NULL DEFAULT 0,

    -- 초안 본문과 gate 기록. 집계에 쓰는 값만 위에 컬럼으로 꺼냈다.
    draft_json         jsonb       NOT NULL,
    grounding_json     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    discarded_json     jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 검토 큐. INSUFFICIENT_EVIDENCE 는 큐에 넣지 않으므로 NULL 이다.
    queued_at          timestamptz,
    -- 기한은 개정 조문의 시행일이다. 확보하지 못했으면 NULL 이며 alerts 가 따로 센다.
    due_at             timestamptz,

    -- 사람의 처리 상태. app_review 만 UPDATE 한다.
    review_state       text        NOT NULL DEFAULT 'PENDING',

    CONSTRAINT impact_assessment_status CHECK (
        status IN ('OK', 'NEEDS_REVIEW', 'INSUFFICIENT_EVIDENCE')
    ),
    CONSTRAINT impact_assessment_review_state CHECK (
        review_state IN ('PENDING', 'ACCEPTED', 'EDITED', 'REJECTED', 'NOT_QUEUED')
    ),
    CONSTRAINT impact_assessment_risk CHECK (risk_level IN ('HIGH', 'MEDIUM', 'LOW')),
    -- 기한이 있으면 큐에 있어야 한다. 반대는 성립하지 않는다 — 시행일을 확보하지
    -- 못한 대기가 있고, 그 사실은 임의의 기본 기한으로 덮지 않는다 (구현 이유 5).
    CONSTRAINT impact_assessment_queue CHECK (
        due_at IS NULL OR queued_at IS NOT NULL
    ),
    -- 큐에 넣지 않은 건은 사람이 처리할 수 없다.
    CONSTRAINT impact_assessment_not_queued CHECK (
        queued_at IS NOT NULL OR review_state = 'NOT_QUEUED'
    )
);

CREATE INDEX impact_assessment_queue_idx
    ON impact_assessment (review_state, due_at)
    WHERE queued_at IS NOT NULL;
CREATE INDEX impact_assessment_thread ON impact_assessment (thread_id);

COMMENT ON TABLE impact_assessment IS
    '영향평가 한 건. 초안 본문은 불변이며 review_state 만 사람이 바꾼다';
COMMENT ON COLUMN impact_assessment.status IS
    'gate 가 정한 기계의 판정. 누구도 UPDATE 할 수 없다 — 사람이 고치면 그 사실이 사라진다';
COMMENT ON COLUMN impact_assessment.review_state IS
    '사람의 처리 상태. 정본은 review_decision 이며 이 컬럼은 큐 질의를 위한 파생이다';
COMMENT ON COLUMN impact_assessment.queued_at IS
    'NULL 이면 검토 큐에 넣지 않은 건(INSUFFICIENT_EVIDENCE). 이관 비율 집계의 기준이다';
COMMENT ON COLUMN impact_assessment.due_at IS
    '개정 조문의 시행일. 우리가 정한 기한이 아니라 밖에서 온 사실이다. 미상이면 NULL';

-- 초안 본문은 사후에 바뀔 수 없다. review_state 만 예외다.
CREATE OR REPLACE FUNCTION reject_assessment_body_mutation() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'impact_assessment 는 DELETE 할 수 없다. 평가 이력은 감사 대상이다';
    END IF;
    IF (to_jsonb(NEW) - 'review_state') IS DISTINCT FROM (to_jsonb(OLD) - 'review_state') THEN
        RAISE EXCEPTION
            'impact_assessment 는 review_state 외의 컬럼을 UPDATE 할 수 없다. '
            '수정은 review_decision.edit_json 에 남긴다 (원칙 6)';
    END IF;
    RETURN NEW;
END
$$;

COMMENT ON FUNCTION reject_assessment_body_mutation() IS
    '초안 본문의 사후 변경을 막는다. 검토자의 수정은 승인 레코드에 남는다';

CREATE TRIGGER impact_assessment_body_immutable
    BEFORE UPDATE OR DELETE ON impact_assessment
    FOR EACH ROW EXECUTE FUNCTION reject_assessment_body_mutation();

-- ---------------------------------------------------------------------------
-- review_decision — 사람의 판단. 이것이 승인의 정본이다 (원칙 4)
-- ---------------------------------------------------------------------------
CREATE TABLE review_decision (
    id                   uuid PRIMARY KEY,
    impact_assessment_id uuid        NOT NULL REFERENCES impact_assessment (id),

    decided_by           text        NOT NULL,
    decision             text        NOT NULL,
    decided_at           timestamptz NOT NULL,

    -- 반려·수정 사유. 닫힌 코드 + 자유 기술 (구현 이유 4).
    reason_code          text,
    reason_note          text,
    -- 수정 승인일 때 무엇을 고쳤는가. 초안 자체는 고치지 않는다.
    edit_json            jsonb,

    -- F-7 감시용. 큐 진입부터 결정까지의 실측 소요.
    reviewed_ms          int         NOT NULL,

    CONSTRAINT review_decision_kind CHECK (decision IN ('ACCEPT', 'EDIT', 'REJECT')),
    CONSTRAINT review_decision_reason_code CHECK (
        reason_code IS NULL OR reason_code IN (
            'WRONG_PARAGRAPH',     -- 지목한 사내 조항이 틀렸다 (F-6)
            'MISSED_PARAGRAPH',    -- 걸리는 조항을 놓쳤다 (F-1)
            'WRONG_DEPARTMENT',    -- 부서 배정이 틀렸다
            'WRONG_RISK',          -- 위험도가 틀렸다
            'NOT_APPLICABLE',      -- 우리 영역이 아니다 — 이관
            'INSUFFICIENT_BASIS',  -- 근거가 주장을 뒷받침하지 않는다 (F-6)
            'OTHER'                -- 자유 기술 필수. 쌓이면 코드 집합을 늘린다
        )
    ),
    -- 반려와 수정에는 사유가 있어야 한다. 수락만 사유 없이 가능하다.
    CONSTRAINT review_decision_reason_required CHECK (
        decision = 'ACCEPT' OR reason_code IS NOT NULL
    ),
    -- OTHER 는 자유 기술을 강제한다. 강제하지 않으면 OTHER 가 기본값이 된다.
    CONSTRAINT review_decision_other_needs_note CHECK (
        reason_code IS DISTINCT FROM 'OTHER'
        OR (reason_note IS NOT NULL AND length(btrim(reason_note)) > 0)
    ),
    CONSTRAINT review_decision_edit_payload CHECK (
        decision <> 'EDIT' OR edit_json IS NOT NULL
    ),
    -- 시계 문제를 조용히 0 으로 만들지 않는다.
    CONSTRAINT review_decision_elapsed CHECK (reviewed_ms >= 0)
);

CREATE INDEX review_decision_assessment ON review_decision (impact_assessment_id, decided_at);
CREATE INDEX review_decision_reason ON review_decision (decision, reason_code);

COMMENT ON TABLE review_decision IS
    '사람의 판단. 승인의 정본이며 사후에 바뀌지 않는다. 발송 대상은 이 행에서만 파생된다';
COMMENT ON COLUMN review_decision.reviewed_ms IS
    '큐 진입부터 결정까지의 실측 소요. F-7(승인 게이트 형식화) 감시 지표다';
COMMENT ON COLUMN review_decision.edit_json IS
    '수정 승인 시 무엇을 고쳤는가. 초안 원본은 고치지 않으므로 둘 다 남는다';

CREATE TRIGGER review_decision_immutable
    BEFORE UPDATE OR DELETE ON review_decision
    FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();

-- ---------------------------------------------------------------------------
-- action_outbox — 발송 워커의 유일한 입력 (원칙 5)
-- ---------------------------------------------------------------------------
-- FK 가 NOT NULL 이다. **승인 레코드 없이 행이 존재할 수 없다** — 권한이 잘못 주어져도
-- 이 제약은 남는다. 권한은 사람이 바꿀 수 있고 제약은 마이그레이션이어야 바뀐다.
CREATE TABLE action_outbox (
    id                   uuid PRIMARY KEY,
    review_decision_id   uuid        NOT NULL REFERENCES review_decision (id),

    action_type          text        NOT NULL,
    -- 발송 워커가 보는 전부. 프롬프트도 모델 출력도 담지 않는다 (원칙 5).
    payload              jsonb       NOT NULL,

    state                text        NOT NULL DEFAULT 'PENDING',
    created_at           timestamptz NOT NULL,
    dispatched_at        timestamptz,

    CONSTRAINT action_outbox_state CHECK (state IN ('PENDING', 'SENT', 'FAILED')),
    CONSTRAINT action_outbox_sent_has_time CHECK (
        state <> 'SENT' OR dispatched_at IS NOT NULL
    )
);

CREATE INDEX action_outbox_pending ON action_outbox (state, created_at);

COMMENT ON TABLE action_outbox IS
    '발송 워커의 유일한 입력. review_decision FK 가 NOT NULL 이라 승인 없이 존재할 수 없다';

-- ---------------------------------------------------------------------------
-- 권한 — 여기가 원칙 5 의 실체다
-- ---------------------------------------------------------------------------
-- app_graph: 초안은 쓸 수 있고 승인과 발송 대상은 쓸 수 없다.
GRANT SELECT, INSERT ON impact_assessment TO app_graph;
GRANT SELECT ON review_decision, action_outbox TO app_graph;

-- app_review: 사람의 판단을 남기고, 그 판단에서 발송 대상을 만든다.
GRANT SELECT ON impact_assessment TO app_review;
GRANT UPDATE (review_state) ON impact_assessment TO app_review;
GRANT SELECT, INSERT ON review_decision TO app_review;
GRANT SELECT, INSERT ON action_outbox TO app_review;

-- app_dispatch: outbox 만 본다. 003 과 같은 경계를 다시 세운다.
GRANT SELECT, UPDATE ON action_outbox TO app_dispatch;

-- app_policy 는 사내 규정 적재 전용이다. 이 테이블들과 무관하다 (009).

-- ---------------------------------------------------------------------------
-- graph_checkpoint — LangGraph 체크포인터 전용 스키마 (기획서 13.4)
-- ---------------------------------------------------------------------------
-- app_graph 가 소유한다. 체크포인터가 자기 테이블을 만들고 갱신·삭제하기 때문이며,
-- 그 권한이 public 스키마의 도메인 테이블 권한과 섞이지 않게 스키마를 나눈다.
CREATE SCHEMA IF NOT EXISTS graph_checkpoint AUTHORIZATION app_graph;

COMMENT ON SCHEMA graph_checkpoint IS
    'LangGraph 체크포인트. 프롬프트와 모델 출력이 들어 있어 app_dispatch 는 USAGE 도 없다';

-- 체크포인트에는 프롬프트와 모델 출력이 있다. 발송 워커는 그것을 보지 않는다 (원칙 5).
REVOKE ALL ON SCHEMA graph_checkpoint FROM PUBLIC;
REVOKE ALL ON SCHEMA graph_checkpoint FROM app_dispatch, app_ingest, app_policy;
-- 검토자는 체크포인트를 직접 읽지 않는다. 검토 화면이 보는 것은 impact_assessment 다 —
-- 직렬화된 그래프 상태를 사람이 읽을 화면에 붙이면 화면이 내부 구조에 결합된다.
REVOKE ALL ON SCHEMA graph_checkpoint FROM app_review;
