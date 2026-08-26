-- ---------------------------------------------------------------------------
-- 009. 사내 규정 코퍼스 — 문서 · 조 단위 문단 · 임베딩, 그리고 적재 role
-- ---------------------------------------------------------------------------
--
-- 002 는 `policy_document` 를 "경계 검증용 최소 정의"로 두고 **"컬럼은 retrieval
-- 착수 시 확정한다"** 고 COMMENT 에 못박았다. 지금이 그때다.
--
-- 목적:
--   사내 내부통제 규정을 조 단위 문단으로 적재하고, 문단마다 임베딩을 붙인다.
--   이 테이블의 문단 ID 집합이 이후 **인용 검증의 유일한 정답 집합**이 된다
--   (원칙 2, 기획서 8.1 2단).
--
-- 구현 이유:
--   1. **문단 단위를 "조"로 고정한다.** 골든셋 `article_spec` 29건이 전부 조 단위이며
--      (`제18조 제3항` 형태가 하나도 없다), 인용 단위가 곧 검증 단위이므로 채점 단위와
--      어긋나게 두면 top-k 안의 두 항이 정밀도상 한 건인지 두 건인지가 되말기 규칙에
--      좌우된다. 지표를 만드는 변환은 넣지 않는다.
--      현재 코퍼스의 조 길이는 중앙값 148자·최대 486자로, 쪼갤 이유가 되는 조가 없다.
--      **실제 은행 규정은 이보다 길다** — 재검토 신호는 `retrieval/corpus.py` 에 적었다.
--   2. **임베딩을 별도 테이블에 둔다.** 모델 2종을 같은 문단에 대해 비교해야 하고
--      (로컬 vs OpenAI), 차원이 다르다(1024 vs 3072). 컬럼 2개로 두면 모델을 추가할
--      때마다 스키마가 바뀌고, 어느 컬럼이 어느 모델인지가 컬럼명이라는 약속에만 남는다.
--   3. **근사 인덱스(HNSW/IVFFlat)를 만들지 않는다.** 근사 탐색은 재현율을 깎으며,
--      지금 재려는 것이 바로 재현율이다. 152행에서 정확 탐색은 밀리초 단위이므로
--      측정에 잡음을 넣는 대신 전수 스캔한다. 코퍼스가 커지면 그때 인덱스를 만들고
--      **근사로 인해 잃는 재현율을 함께 측정해서** 기록한다.
--   4. **`app_policy` role 을 새로 만든다.** 기존 4종 중 어느 것도 이 테이블에 쓰면
--      안 된다 — `app_ingest` 는 법령 수집 경로이고 사내 정책을 읽을 이유조차 없으며
--      (002 주석, 직무분리), `app_graph` 는 읽기 전용이다(원칙 5). 쓰기 주체가 없다는
--      이유로 소유자 계정으로 적재하면 경계가 통째로 사라진다.
--
-- 트레이드오프:
--   - `policy_document` 를 DROP 하고 다시 만든다. 002 가 "데이터가 없으므로 재정의해도
--     된다"고 미리 적어 둔 경로다. 003 의 GRANT 는 재실행 가능하게 쓰여 있고,
--     `ALTER DEFAULT PRIVILEGES` 로 app_graph/app_review 의 SELECT 는 자동 승계된다.
--   - 임베딩 테이블에는 bitemporal 을 적용하지 않고 UPDATE/DELETE 를 허용한다.
--     임베딩은 `text_norm` 에서 결정론적으로 파생되는 **캐시**이며, 감사 질문
--     ("그 시점에 담당자가 알 수 있었던 정보는 무엇이었나")의 답은 벡터가 아니라
--     원문과 `llm_invocation.retrieved_chunk_ids` 다. 벡터를 불변으로 두면 모델을
--     바꿀 때마다 지울 수 없는 행이 쌓이고, 그 행들은 아무 질문에도 답하지 않는다.
--   - `embedding` 컬럼에 차원 제약을 걸지 않는다(`vector` typmod 없음). 모델마다
--     차원이 다르기 때문이며, 대신 `dim` 컬럼과 CHECK 로 행 단위 정합성을 잡는다.
--     잃는 것은 컬럼 수준의 차원 보장이고, 얻는 것은 모델 추가 시 스키마 무변경이다.
--
-- 엣지 케이스:
--   - 같은 문서의 새 버전 적재: `version` 이 자연키에 들어 있어 별도 행이 된다.
--     과거 버전을 닫지 않는다 — 개정 이력이 곧 코퍼스다.
--   - 같은 버전 재적재: 열린 행의 부분 유니크 인덱스가 충돌시킨다. 조용히 덮어쓰지
--     않는다. 재적재는 `known_until` 을 닫고 새 행을 넣는 정정 절차다.
--   - `effective_date` 가 없는 문서: NOT NULL 이므로 적재가 실패한다. 시점 검색
--     (`as_of`)이 그 값을 쓰므로, 없는 채로 들어가면 시점 질의가 조용히 그 문서를
--     빠뜨리거나 항상 포함시킨다.
--   - 조 번호 중복: `(document_id, article_no)` 유니크. 파서가 이미 거부하지만
--     DB 에서도 막는다 — 파서를 우회한 적재 경로가 나중에 생길 수 있다.
--   - 임베딩만 있고 문단이 사라짐: FK ON DELETE CASCADE 를 걸지 않는다. 문단은
--     bitemporal 이라 삭제되지 않으므로 CASCADE 는 일어날 수 없는 일을 위한 장치다.

CREATE EXTENSION IF NOT EXISTS vector;

-- 002 의 경계 검증용 최소 정의를 대체한다.
DROP TABLE IF EXISTS policy_document;

-- ---------------------------------------------------------------------------
-- policy_document — 사내 규정 문서 한 버전
-- ---------------------------------------------------------------------------
CREATE TABLE policy_document (
    id                 uuid PRIMARY KEY,

    doc_id             text        NOT NULL,
    version            text        NOT NULL,
    title              text        NOT NULL,
    owner_dept         text        NOT NULL,
    classification     text        NOT NULL,

    -- 이 버전이 시행된 날. 시점 검색(`as_of`)의 기준이며 문서 자체의 valid_from 이다.
    effective_date     date        NOT NULL,

    -- 이 문서가 근거로 삼는 법령. 조문 경로가 아니라 법령명 목록이다 —
    -- 조문 단위 연결은 3단계 검색 결과와 4단계 영향평가가 만든다.
    parent_laws        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    revision_history   jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 출처 추적 (ADR-007). 원본 파일과 그 내용 해시.
    source_path        text        NOT NULL,
    source_sha256      char(64)    NOT NULL,

    known_from         timestamptz NOT NULL,
    known_until        timestamptz NOT NULL DEFAULT 'infinity',

    CONSTRAINT policy_document_known_range CHECK (known_from < known_until)
);

CREATE UNIQUE INDEX policy_document_current_key
    ON policy_document (doc_id, version)
    WHERE known_until = 'infinity';

CREATE INDEX policy_document_as_of
    ON policy_document (effective_date, known_from, known_until);

COMMENT ON TABLE policy_document IS
    '사내 규정 문서 한 버전. 조 단위 문단은 policy_paragraph 가 갖는다';
COMMENT ON COLUMN policy_document.effective_date IS
    '이 버전의 시행일. 시점 검색(as_of)의 기준이다';

CREATE TRIGGER policy_document_immutable
    BEFORE UPDATE OR DELETE ON policy_document
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- policy_paragraph — 조 하나. 검색·인용·검증의 공통 단위
-- ---------------------------------------------------------------------------
CREATE TABLE policy_paragraph (
    id                 uuid PRIMARY KEY,
    document_id        uuid        NOT NULL REFERENCES policy_document (id),

    -- 자연키. `제18조` 의 18.
    article_no         int         NOT NULL,
    article_title      text        NOT NULL,

    -- 문서 안의 등장 순서. article_no 와 같아야 정상이지만 별도로 둔다 —
    -- 어긋나면 파서가 조 번호를 잘못 읽은 것이고, 그 사실이 보여야 한다.
    seq_in_doc         int         NOT NULL,

    -- 인용이 가리키는 것은 `text_raw` 다. 담당자가 원문을 열었을 때 그대로 있어야
    -- 한다. `text_norm` 은 검색 인덱스의 입력이며 화면에 보이지 않는다 (ADR-002).
    text_raw           text        NOT NULL,
    text_norm          text        NOT NULL,
    text_norm_sha256   char(64)    NOT NULL,
    norm_rule_version  text        NOT NULL,

    known_from         timestamptz NOT NULL,
    known_until        timestamptz NOT NULL DEFAULT 'infinity',

    CONSTRAINT policy_paragraph_known_range CHECK (known_from < known_until),
    CONSTRAINT policy_paragraph_article_no_positive CHECK (article_no > 0)
);

CREATE UNIQUE INDEX policy_paragraph_current_key
    ON policy_paragraph (document_id, article_no)
    WHERE known_until = 'infinity';

CREATE INDEX policy_paragraph_known
    ON policy_paragraph (known_from, known_until);

COMMENT ON TABLE policy_paragraph IS
    '조 단위 문단. 검색 결과 ID 집합이 인용 검증의 정답 집합이 된다 (원칙 2)';
COMMENT ON COLUMN policy_paragraph.text_raw IS
    '인용이 가리키는 원문. 정규화본은 검색 인덱스 입력일 뿐이다 (ADR-002)';

CREATE TRIGGER policy_paragraph_immutable
    BEFORE UPDATE OR DELETE ON policy_paragraph
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- policy_paragraph_embedding — 파생 캐시. bitemporal 이 아니다
-- ---------------------------------------------------------------------------
CREATE TABLE policy_paragraph_embedding (
    paragraph_id  uuid        NOT NULL REFERENCES policy_paragraph (id),

    -- 어떤 모델로 만든 벡터인가. 비교 측정의 축이며 재현의 근거다.
    model_id      text        NOT NULL,
    dim           int         NOT NULL,
    embedding     vector      NOT NULL,

    embedded_at   timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (paragraph_id, model_id),
    CONSTRAINT policy_paragraph_embedding_dim CHECK (vector_dims(embedding) = dim)
);

COMMENT ON TABLE policy_paragraph_embedding IS
    'text_norm 에서 파생된 캐시. 감사 근거는 벡터가 아니라 원문과 retrieved_chunk_ids 다';

-- ---------------------------------------------------------------------------
-- app_policy — 사내 규정 적재 전용 role
-- ---------------------------------------------------------------------------
-- 법령 테이블에 SELECT 조차 주지 않는다. 규정 적재가 법령을 읽을 이유가 없고,
-- 읽을 수 있으면 나중에 "여기서 같이 처리하면 편한데"가 반드시 생긴다.
DO $$
BEGIN
    CREATE ROLE app_policy LOGIN PASSWORD 'app_policy_local_dev_only';
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'role app_policy already exists, skipping';
END
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_policy;
REVOKE ALL ON SCHEMA public FROM app_policy;
-- 데이터베이스 이름을 하드코딩하지 않는다 (003 과 같은 이유 — CI·관리형 인스턴스에서
-- 이름이 다르면 이 문장만 조용히 대상을 잃는다).
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_policy', current_database());
END
$$;
GRANT USAGE ON SCHEMA public TO app_policy;

GRANT SELECT, INSERT ON policy_document, policy_paragraph TO app_policy;
-- 임베딩은 파생 캐시이므로 갱신·삭제를 허용한다 (위 트레이드오프 참조).
GRANT SELECT, INSERT, UPDATE, DELETE ON policy_paragraph_embedding TO app_policy;
-- 정정(known_until 닫기)은 운영 절차이며 적재 경로의 권한이 아니다.
-- app_ingest 에 UPDATE 를 주지 않은 것과 같은 판단이다 (003).

-- app_graph 는 003 의 ALTER DEFAULT PRIVILEGES 로 새 테이블 SELECT 를 승계한다.
-- 명시적으로 한 번 더 준다 — DEFAULT PRIVILEGES 는 테이블을 만든 role 에 종속되므로
-- 다른 role 이 마이그레이션을 돌리면 조용히 적용되지 않는다.
GRANT SELECT ON policy_document, policy_paragraph, policy_paragraph_embedding TO app_graph;
GRANT SELECT ON policy_document, policy_paragraph, policy_paragraph_embedding TO app_review;
