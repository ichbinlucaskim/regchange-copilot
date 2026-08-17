-- 002 — 권한 경계를 지금 검증하기 위한 최소 테이블 정의
--
-- 목적:
--   role 4종의 권한 경계(원칙 5)를 **지금** 테스트로 고정한다. 경계의 반대편에
--   테이블이 없으면 "거부되는지"를 검사할 수 없고, 검사하지 못한 경계는 없는 경계다.
--
-- 구현 이유:
--   0.5단계 보안 테스트는 `CREATE TABLE` 거부만 검사했고, 그것은 Postgres 15+ 에서
--   INSERT/UPDATE/DELETE 를 전부 가진 role 도 통과한다(PUBLIC 의 public 스키마 CREATE
--   권한이 회수되어 있으므로). 즉 "읽기 전용임"이 아니라 "스키마 소유자가 아님"만
--   증명했다. 실제로 검사할 것을 검사하려면 대상 테이블이 있어야 한다.
--
--   컬럼은 각 작업(4: diff, 5: 검토/발송)에서 확정한다. 여기 있는 것은 경계가
--   걸리는 지점(테이블 이름과 승인 관련 컬럼)뿐이다.
--
-- 트레이드오프:
--   설계가 확정되지 않은 테이블이 스키마에 먼저 생긴다. CLAUDE.md §10 의 "검증 없이
--   쌓지 않는다"와 긴장 관계에 있다. 그럼에도 지금 만드는 이유는, 이 테이블들이
--   담는 것이 **데이터가 아니라 경계**이기 때문이다. 데이터가 없으므로 나중에
--   컬럼을 바꾸는 비용은 0이고, 경계를 나중에 거는 비용은 "그 사이에 쓰인 코드가
--   경계를 넘어도 아무도 몰랐다"이다.
--   그 대신 이 파일이 방치되면 실제 스키마인 척하게 되므로, 각 테이블에 어느 작업이
--   정의를 확정하는지 COMMENT 로 못박는다.
--
-- 엣지 케이스:
--   - 나중에 컬럼이 추가될 때: 데이터가 없으므로 ALTER 가 아니라 재정의해도 된다.
--     단 GRANT 는 테이블 단위이므로 DROP/CREATE 하면 003 의 GRANT 를 다시 실행해야
--     한다. 그래서 003 은 재실행 가능하게 썼다.
--   - `impact_assessment.status` 는 지금 정의한다. app_review 의 컬럼 단위 UPDATE
--     권한이 이 컬럼에 걸리므로, 이름이 없으면 경계 자체를 표현할 수 없다.

-- 작업 4(diff)에서 정의를 확정한다.
CREATE TABLE change_set (
    id           uuid PRIMARY KEY,
    detected_at  timestamptz NOT NULL,
    note         text
);
COMMENT ON TABLE change_set IS '경계 검증용 최소 정의. 컬럼은 작업 4(diff)에서 확정한다';

CREATE TABLE article_change (
    id            uuid PRIMARY KEY,
    change_set_id uuid REFERENCES change_set (id),
    article_id    uuid REFERENCES regulation_article (id),
    change_type   text NOT NULL
);
COMMENT ON TABLE article_change IS '경계 검증용 최소 정의. 컬럼은 작업 4(diff)에서 확정한다';

CREATE TABLE article_move_candidate (
    id            uuid PRIMARY KEY,
    change_set_id uuid REFERENCES change_set (id),
    score         real,
    -- ADR-003: 이동을 자동 확정하지 않는다. 확정은 검토자가 한다.
    confirmed_by  text
);
COMMENT ON TABLE article_move_candidate IS
    '경계 검증용 최소 정의. ADR-003 에 따라 자동 확정 경로를 만들지 않는다';

-- 사내 규정 문서. app_ingest 는 이 테이블에 접근할 수 없어야 한다 —
-- 법령 수집 경로가 사내 정책을 읽을 이유가 없다 (직무분리, 축 2).
CREATE TABLE policy_document (
    id      uuid PRIMARY KEY,
    title   text NOT NULL,
    body    text NOT NULL
);
COMMENT ON TABLE policy_document IS '경계 검증용 최소 정의. 컬럼은 retrieval 착수 시 확정한다';

CREATE TABLE impact_assessment (
    id          uuid PRIMARY KEY,
    -- app_review 는 이 컬럼만 UPDATE 할 수 있다. 컬럼 단위 GRANT 의 대상이다.
    status      text NOT NULL DEFAULT 'DRAFT',
    body        text
);
COMMENT ON TABLE impact_assessment IS
    '경계 검증용 최소 정의. status 는 app_review 의 컬럼 단위 UPDATE 대상이다';

CREATE TABLE review_decision (
    id                   uuid PRIMARY KEY,
    impact_assessment_id uuid REFERENCES impact_assessment (id),
    decided_by           text NOT NULL,
    decision             text NOT NULL,
    decided_at           timestamptz NOT NULL
);
COMMENT ON TABLE review_decision IS '경계 검증용 최소 정의. 컬럼은 작업 5(승인)에서 확정한다';

-- 발송 워커가 보는 유일한 테이블. 승인 레코드에서 파생되며, 프롬프트나 모델
-- 출력을 담지 않는다 (원칙 5).
CREATE TABLE action_outbox (
    id                   uuid PRIMARY KEY,
    review_decision_id   uuid REFERENCES review_decision (id),
    state                text NOT NULL DEFAULT 'PENDING',
    dispatched_at        timestamptz
);
COMMENT ON TABLE action_outbox IS
    '발송 워커의 유일한 입력. 프롬프트·모델 출력을 담지 않는다 (원칙 5)';

-- LLM 호출 기록과 감사 이벤트. app_graph 가 INSERT 할 수 있는 유일한 두 테이블이다.
CREATE TABLE llm_invocation (
    id          uuid PRIMARY KEY,
    invoked_at  timestamptz NOT NULL,
    model       text NOT NULL,
    note        text
);
COMMENT ON TABLE llm_invocation IS '경계 검증용 최소 정의. app_graph 가 INSERT 할 수 있다';

CREATE TABLE audit_event (
    id           uuid PRIMARY KEY,
    occurred_at  timestamptz NOT NULL,
    event_type   text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb
);
COMMENT ON TABLE audit_event IS '경계 검증용 최소 정의. app_graph 가 INSERT 할 수 있다';
