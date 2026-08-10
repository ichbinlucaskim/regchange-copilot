-- 로컬 Postgres 최초 기동 시 1회 실행된다 (볼륨이 비어 있을 때만).
--
-- 목적:
--   pgvector 확장을 설치하고, LLM 프로세스가 사용할 읽기 전용 role을 만든다.
--
-- 구현 이유:
--   원칙 5(LLM 프로세스는 DB 읽기 전용 role을 쓴다)의 경계를 애플리케이션
--   코드가 아니라 DB 권한으로 세운다. 코드로 세운 경계는 코드 수정으로
--   무너지지만, 권한으로 세운 경계는 DBA 개입 없이는 무너지지 않는다.
--   로컬 환경에도 같은 경계를 두어야 security 테스트가 개발 중에 돈다.
--
-- 트레이드오프:
--   로컬 개발자가 "왜 INSERT가 안 되지"로 한 번 막힌다. 그 마찰은 의도된
--   것이다. 개발 환경에서 통과하던 코드가 운영에서만 권한 오류를 내는 쪽이
--   훨씬 비싸다.
--
-- 엣지 케이스:
--   - 확장이 이미 있는 경우: IF NOT EXISTS 로 무시한다
--   - role이 이미 있는 경우: DO 블록에서 duplicate_object 를 잡아 넘어간다
--   - 이후 생성될 테이블: DEFAULT PRIVILEGES 로 SELECT 만 자동 부여한다
--   - 운영 환경: 이 파일은 실행되지 않는다. 관리형 DB의 권한 설정은 별도
--     문서(docs/08-deployment-considerations.md)에서 다룬다

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    CREATE ROLE regchange_llm_ro LOGIN PASSWORD 'regchange_ro_local_dev_only';
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'role regchange_llm_ro already exists, skipping';
END
$$;

-- 접속과 조회만 허용한다.
GRANT CONNECT ON DATABASE regchange TO regchange_llm_ro;
GRANT USAGE ON SCHEMA public TO regchange_llm_ro;

-- 객체 생성 권한을 명시적으로 회수한다. PUBLIC 에서도 회수해야
-- regchange_llm_ro 가 PUBLIC 을 통해 CREATE 를 얻지 못한다.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM regchange_llm_ro;

-- 기존 및 앞으로 생성될 테이블/시퀀스에 대해 읽기만 부여한다.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO regchange_llm_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO regchange_llm_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO regchange_llm_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO regchange_llm_ro;
