-- 003 — DB role 4종과 권한 경계 (원칙 5)
--
-- 목적:
--   프로세스마다 다른 role 로 접속하게 하고, 각 role 이 할 수 있는 일을 DB 권한으로
--   고정한다. 프롬프트 인젝션이나 코드 버그로 경계를 넘으려 해도 DB 가 거부한다.
--
-- 구현 이유:
--   애플리케이션 조건문으로 세운 경계는 코드 수정으로 무너진다. 권한으로 세운 경계는
--   DBA 개입 없이는 무너지지 않는다 (원칙 5). 특히 아래 두 가지는 코드가 아니라
--   권한으로만 강제할 수 있다:
--
--   1. **app_ingest 에 ministry_master INSERT 를 주지 않는다.** 미지의 부처명을
--      마스터에 자동 등재하지 않기로 한 결정(ADR-009, 자동 병합 금지와 같은 성질)을
--      권한으로 못박는다. 코드에 "자동 등재" 분기를 실수로 넣어도 DB 가 거부한다.
--   2. **app_dispatch 는 action_outbox 외 어떤 테이블도 볼 수 없다.** 발송 워커가
--      프롬프트·모델 출력을 보지 않는다는 원칙 5의 경계는 "import 하지 않는다"로는
--      부족하다. SQL 은 import 없이도 읽을 수 있다.
--
-- 트레이드오프:
--   role 이 늘어 접속 문자열과 운영 문서가 복잡해진다. 개발 중에 "왜 이 쿼리가
--   안 되지"로 한 번씩 막힌다. 그 마찰은 의도된 것이다 — 개발에서 통과하고 운영에서만
--   권한 오류가 나는 쪽이 훨씬 비싸다.
--   또한 이 파일은 로컬 개발용 비밀번호를 담는다. 운영 환경의 권한 설정은 이 파일이
--   아니라 docs/08-deployment-considerations.md 가 다룬다.
--
-- 엣지 케이스:
--   - role 이 이미 있는 경우: duplicate_object 를 잡고 넘어간다.
--   - 재실행: 각 role 에서 ALL 을 REVOKE 한 뒤 다시 GRANT 한다. 선언적으로 수렴한다.
--   - 앞으로 생길 테이블: app_graph / app_review 에만 DEFAULT PRIVILEGES 로 SELECT 를
--     준다. app_ingest·app_dispatch 에는 주지 않는다 — 새 테이블이 생겼다는 이유로
--     수집·발송의 시야가 넓어지면 안 된다.
--   - 0.5단계의 `regchange_llm_ro`: app_graph 로 대체됐다. 기존 접속 문자열이 깨지지
--     않도록 남겨 두되 새로 권한을 주지 않는다. 읽기 전용이므로 남아 있어도
--     경계를 넓히지 않는다.

DO $$
DECLARE
    spec record;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('app_ingest',   'app_ingest_local_dev_only'),
            ('app_graph',    'app_graph_local_dev_only'),
            ('app_review',   'app_review_local_dev_only'),
            ('app_dispatch', 'app_dispatch_local_dev_only')
        ) AS t(role_name, role_password)
    LOOP
        BEGIN
            EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', spec.role_name, spec.role_password);
        EXCEPTION
            WHEN duplicate_object THEN
                RAISE NOTICE 'role % already exists, skipping', spec.role_name;
        END;
    END LOOP;
END
$$;

-- 재실행 시 선언적으로 수렴하도록 먼저 전부 회수한다.
DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['app_ingest', 'app_graph', 'app_review', 'app_dispatch']
    LOOP
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', role_name);
        EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
        -- 데이터베이스 이름을 하드코딩하지 않는다. 로컬은 regchange 지만 관리형
        -- 인스턴스나 CI 에서는 다를 수 있고, 그때 이 문장만 조용히 대상을 잃는다.
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), role_name);
        EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', role_name);
    END LOOP;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- app_ingest — 법령 수집·적재
-- ---------------------------------------------------------------------------
-- 규제 테이블에 INSERT 한다. 정책 문서와 outbox 는 볼 수 없다.
-- UPDATE 를 주지 않는 이유: 적재 경로는 INSERT 만 한다. 정정(known_until 을 닫고 새
-- 행을 INSERT)은 운영 절차이며 별도 권한으로 다룬다. 지금 UPDATE 를 주면 트리거가
-- 허용하는 "닫는 UPDATE" 하나 때문에 수집 프로세스가 정정 능력을 갖게 된다.
GRANT SELECT, INSERT ON regulation_document, regulation_article TO app_ingest;
GRANT SELECT, INSERT ON ministry_unresolved, load_run TO app_ingest;
-- 마스터는 읽기만. 자동 등재를 권한으로 차단한다 (ADR-009).
GRANT SELECT ON ministry_master TO app_ingest;

-- ---------------------------------------------------------------------------
-- app_graph — LangGraph / LLM 경로 (원칙 5)
-- ---------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_graph;
GRANT INSERT ON llm_invocation, audit_event TO app_graph;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_graph;

-- ---------------------------------------------------------------------------
-- app_review — 검토자 (원칙 4)
-- ---------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_review;
GRANT INSERT ON review_decision TO app_review;
-- 컬럼 단위 UPDATE. 검토자는 상태만 바꾸고 본문은 고치지 않는다.
GRANT UPDATE (status) ON impact_assessment TO app_review;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_review;

-- ---------------------------------------------------------------------------
-- app_dispatch — 발송 워커 (원칙 5)
-- ---------------------------------------------------------------------------
-- 승인 레코드에서 파생된 outbox 만 본다. 다른 테이블에 SELECT 도 주지 않는다.
GRANT SELECT, UPDATE ON action_outbox TO app_dispatch;
