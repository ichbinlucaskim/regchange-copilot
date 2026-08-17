-- 001 — bitemporal 코어 테이블 (규제 문서 · 조문 · 조직 마스터 · 적재 run)
--
-- 목적:
--   법령 본문을 조문 단위로 적재하고, 그 조문이 "현실에서 언제 효력이 있었는가"
--   (valid_time)와 "우리가 언제 그것을 알고 있었는가"(transaction_time)를 함께
--   기록한다. 감사 재현 질의(그 시점에 담당자가 볼 수 있었던 조문)의 저장 기반이다.
--
-- 구현 이유:
--   두 시간축을 컬럼으로 나누고 UPDATE 를 트리거로 차단한다. 애플리케이션에서
--   "덮어쓰지 않기"를 규율로 지키는 대안은 6개월 뒤 급한 수정 한 번으로 무너진다.
--   원칙 6은 코드가 아니라 DB 제약으로 강제해야 한다 (원칙 5의 권한 경계와 같은 발상).
--
--   시각과 달력을 섞지 않는다. valid_time 은 date — 법정 달력이며 타임존 변환
--   대상이 아니다. transaction_time 은 timestamptz — 우리 시각이므로 UTC 다.
--   같은 컬럼에 두 성질을 담으면 "2026-03-15 시점"이 서버 타임존에 따라 다른 조문
--   집합을 반환하게 되고, 그 차이는 감사에서 재현 불가로 나타난다.
--
-- 트레이드오프:
--   현재 행을 고르려면 모든 질의가 `known_until = 'infinity'`를 달아야 한다.
--   조건 하나를 빠뜨리면 닫힌 과거 행이 결과에 섞이고, 그 오류는 예외가 아니라
--   중복 행으로 조용히 나타난다. 이 위험을 감수하는 이유는 반대 방향의 손실이
--   복구 불가능하기 때문이다 — 덮어쓴 과거는 되살릴 수 없다.
--   UNIQUE 를 부분 인덱스(`WHERE known_until = 'infinity'`)로 거는 것도 같은 성질의
--   비용이다. 전체 유니크가 아니므로 닫힌 행들 사이의 정합은 제약이 아니라
--   트리거와 적재 코드가 지킨다.
--
-- 엣지 케이스:
--   - 정정: UPDATE 가 아니라 `known_until` 을 닫고 새 행을 INSERT 한다.
--     트리거가 닫는 UPDATE 하나만 허용하고 나머지를 전부 거부한다.
--   - 같은 MST 가 시행일 3개로 갈리는 경우(edge-case #18 "시행일 버전"): 서로 다른
--     문서 행이다. 중복이 아니므로 제거하지 않는다.
--   - `valid_from` 이 NULL 인 조문: 본문 API 만 적재한 상태다. 시행일을 모르는 것이지
--     오늘 시행 중인 것이 아니므로, 시점 질의에서 NULL 은 어느 시점에도 매칭되지
--     않는다. 이것이 의도된 동작이다.
--   - `document_effective_date` 가 없는 응답: 버전을 식별할 수 없어 유니크 키가
--     무너진다. NOT NULL 로 적재 시점에 실패시킨다. 파서 버그를 숨기지 않는다.

-- ---------------------------------------------------------------------------
-- 불변성 트리거 — 원칙 6을 DB 로 강제한다
-- ---------------------------------------------------------------------------
-- SQLSTATE 를 직접 지정하는 이유: 테스트가 메시지 문자열이 아니라 코드로 단언할 수
-- 있어야 한다. 메시지로 단언하면 문구를 다듬는 순간 보안 테스트가 조용히 통과한다.
--   RC001 = text_raw / text_norm 변경 시도
--   RC002 = 허용되지 않은 UPDATE (닫는 UPDATE 외 전부)
--   RC003 = DELETE 시도
CREATE OR REPLACE FUNCTION reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_row jsonb;
    new_row jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            '% 는 DELETE 할 수 없다. 과거 레코드를 지우면 감사 재현이 불가능해진다 (원칙 6)',
            TG_TABLE_NAME
            USING ERRCODE = 'RC003';
    END IF;

    old_row := to_jsonb(OLD);
    new_row := to_jsonb(NEW);

    -- 텍스트 불변을 먼저 검사한다. 아래의 일반 검사에도 걸리지만, 별도 코드로
    -- 구별해야 "무엇을 어기려 했는가"가 로그와 테스트에 남는다.
    IF old_row ? 'text_raw' THEN
        IF (old_row ->> 'text_raw') IS DISTINCT FROM (new_row ->> 'text_raw')
           OR (old_row ->> 'text_norm') IS DISTINCT FROM (new_row ->> 'text_norm') THEN
            RAISE EXCEPTION
                'text_raw / text_norm 은 UPDATE 할 수 없다. 정정은 known_until 을 닫고 새 행을 INSERT 한다 (ADR-002, 원칙 6)'
                USING ERRCODE = 'RC001';
        END IF;
    END IF;

    IF OLD.known_until <> 'infinity'::timestamptz THEN
        RAISE EXCEPTION
            '이미 닫힌 행(known_until=%)은 다시 UPDATE 할 수 없다', OLD.known_until
            USING ERRCODE = 'RC002';
    END IF;

    IF NEW.known_until = 'infinity'::timestamptz THEN
        RAISE EXCEPTION
            '허용되는 UPDATE 는 known_until 을 닫는 것 하나뿐이다'
            USING ERRCODE = 'RC002';
    END IF;

    -- known_until 외의 컬럼이 하나라도 바뀌면 거부한다. 컬럼 이름을 나열하지 않는
    -- 이유는 나중에 컬럼이 늘 때 트리거를 함께 고치는 것을 잊으면 그 컬럼만 조용히
    -- 수정 가능해지기 때문이다. 나열하지 않으면 잊을 것이 없다.
    IF (old_row - 'known_until') <> (new_row - 'known_until') THEN
        RAISE EXCEPTION
            'known_until 외의 컬럼은 UPDATE 할 수 없다. 정정은 새 행을 INSERT 한다 (원칙 6)'
            USING ERRCODE = 'RC002';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION reject_history_mutation() IS
    'bitemporal 테이블의 UPDATE/DELETE 를 차단한다. known_until 을 닫는 UPDATE 만 허용 (원칙 6)';

-- ---------------------------------------------------------------------------
-- regulation_document — 법령 버전(MST) 하나
-- ---------------------------------------------------------------------------
CREATE TABLE regulation_document (
    id                       uuid PRIMARY KEY,

    law_id                   text        NOT NULL,
    mst                      text        NOT NULL,
    law_name                 text        NOT NULL,
    law_kind                 text,
    ministry_code            text,
    ministry_name_observed   text,

    promulgation_date        date,

    -- 문서 시행일자. **valid_from 이 아니다.**
    -- 본문 API 는 모든 조문의 시행일을 이 값으로 평탄화한다 (edge-case #8, ADR-005).
    -- 조문 단위 valid_from 은 이력 API 에서만 온다. 이 값을 valid_from 으로 승격하는
    -- 코드가 들어오면 원칙 6이 무너진다.
    document_effective_date  date        NOT NULL,

    -- 출처 추적: 어느 스냅샷의 어느 매니페스트에서 왔는가
    source_key               text        NOT NULL,
    source_run_id            text        NOT NULL,
    source_page_sha256       char(64)    NOT NULL,

    load_run_id              uuid        NOT NULL,

    known_from               timestamptz NOT NULL,
    known_until              timestamptz NOT NULL DEFAULT 'infinity',

    CONSTRAINT regulation_document_known_range CHECK (known_from < known_until)
);

-- 현재 행(열린 행)만 유일하다. 닫힌 과거 행은 같은 자연키로 여러 개 존재한다 —
-- 그것이 정정 이력이다.
CREATE UNIQUE INDEX regulation_document_current_key
    ON regulation_document (law_id, mst, document_effective_date)
    WHERE known_until = 'infinity';

CREATE INDEX regulation_document_known ON regulation_document (known_from, known_until);

COMMENT ON COLUMN regulation_document.document_effective_date IS
    '문서 시행일자. 조문 valid_from 이 아니다 — 본문 API 는 조문별 시행일을 이 값으로 평탄화한다 (edge-case #8)';

CREATE TRIGGER regulation_document_immutable
    BEFORE UPDATE OR DELETE ON regulation_document
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- regulation_article — 조문 하나 (ADR-001)
-- ---------------------------------------------------------------------------
CREATE TABLE regulation_article (
    id                 uuid PRIMARY KEY,
    document_id        uuid        NOT NULL REFERENCES regulation_document (id),

    -- ADR-001: 자연키는 (document_id, article_key, seq_in_doc)
    article_key        char(7)     NOT NULL,
    seq_in_doc         int         NOT NULL,
    unit_type          text        NOT NULL,
    article_no         int         NOT NULL,
    branch_no          int         NOT NULL DEFAULT 0,
    title              text,

    -- ADR-002: 원문과 정규화본을 둘 다 저장한다. sha256 은 정규화본의 해시다.
    text_raw           text        NOT NULL,
    text_norm          text        NOT NULL,
    text_norm_sha256   char(64)    NOT NULL,
    norm_rule_version  text        NOT NULL,
    amendment_markers  jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 항/호/목 트리. 문단 단위 테이블은 retrieval 착수 시 만든다. 지금 만들면
    -- 인용 단위를 검색 설계 전에 고정하게 되므로, 손실 없이 보존만 해 둔다.
    body               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    heading_path       text[]      NOT NULL DEFAULT '{}',

    -- ADR-007: 값의 출처는 값의 속성이다
    article_key_source text        NOT NULL DEFAULT 'API',

    -- valid_time — 법정 달력
    valid_from         date,
    valid_to           date        NOT NULL DEFAULT DATE '9999-12-31',
    valid_from_source  text        NOT NULL,

    -- transaction_time — 우리 시각
    known_from         timestamptz NOT NULL,
    known_until        timestamptz NOT NULL DEFAULT 'infinity',

    load_run_id        uuid        NOT NULL,

    CONSTRAINT regulation_article_unit_type
        CHECK (unit_type IN ('ARTICLE', 'HEADING')),
    CONSTRAINT regulation_article_key_source
        CHECK (article_key_source IN ('API', 'PARSED')),
    -- PENDING_HISTORY = 아직 이력 API 를 결합하지 않았다. valid_from 은 NULL 이다.
    -- HISTORY_API     = 일자별 조문 개정 이력의 조문시행일에서 왔다.
    -- 본문의 조문시행일자가 valid_from 이 되는 경로는 **존재하지 않는다**.
    CONSTRAINT regulation_article_valid_from_source
        CHECK (valid_from_source IN ('PENDING_HISTORY', 'HISTORY_API')),
    -- 출처와 값이 어긋나는 상태를 스키마가 거부한다. PENDING 인데 값이 있으면
    -- 어딘가에서 문서 시행일을 승격한 것이다.
    CONSTRAINT regulation_article_valid_from_consistency CHECK (
        (valid_from_source = 'PENDING_HISTORY' AND valid_from IS NULL)
        OR (valid_from_source = 'HISTORY_API' AND valid_from IS NOT NULL)
    ),
    CONSTRAINT regulation_article_valid_range
        CHECK (valid_from IS NULL OR valid_from < valid_to),
    CONSTRAINT regulation_article_known_range CHECK (known_from < known_until),
    CONSTRAINT regulation_article_key_length CHECK (length(article_key) = 7)
);

CREATE UNIQUE INDEX regulation_article_current_key
    ON regulation_article (document_id, article_key, seq_in_doc)
    WHERE known_until = 'infinity';

-- 시점 질의 3종이 타는 인덱스. valid_time 과 transaction_time 을 함께 건다 —
-- 감사 재현(질의 3)은 네 컬럼을 모두 좁히므로 한쪽만 걸면 절반은 필터로 남는다.
CREATE INDEX regulation_article_bitemporal
    ON regulation_article (valid_from, valid_to, known_from, known_until);
CREATE INDEX regulation_article_document ON regulation_article (document_id);

COMMENT ON COLUMN regulation_article.valid_from IS
    '조문별 시행일. 출처는 일자별 조문 개정 이력 API 뿐이다 (ADR-005). NULL 은 미결합 상태';

CREATE TRIGGER regulation_article_immutable
    BEFORE UPDATE OR DELETE ON regulation_article
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- ministry_master — 소관부처 코드/명칭 (ADR-009)
-- ---------------------------------------------------------------------------
-- 컬럼명은 ADR-009 가 적은 그대로 둔다(valid_until). 조문 테이블의 valid_to 와
-- 다른 이름인 것은 의도적이다 — ADR 과 스키마가 어긋나면 어느 쪽이 원본인지
-- 6개월 뒤에 판단할 수 없다.
CREATE TABLE ministry_master (
    id            uuid PRIMARY KEY,

    org_code      text,
    org_name      text        NOT NULL,

    -- 이 값을 어느 필드에서 관측했는가. 추론하지 않는다.
    --   소관부처명   = 부처 이름 그 자체
    --   법령구분명   = '환경부령' 처럼 부처명이 포함된 법령 종류. 부처명이 아니다
    source_field  text        NOT NULL,

    --   OBSERVED_FLATTENED = 0.8 캐시의 (소관부처코드, 소관부처명). API 가 과거 행에도
    --                        현재 이름을 넣어 돌려주므로 **과거로 소급하지 않는다**
    --   OBSERVED_BOUNDARY  = 법령구분명에서 실측된 이름 변경 경계. 시점은 있으나
    --                        org_code 가 없다
    source        text        NOT NULL,

    valid_from    date        NOT NULL,
    valid_until   date,

    known_from    timestamptz NOT NULL,
    known_until   timestamptz NOT NULL DEFAULT 'infinity',

    note          text,

    CONSTRAINT ministry_master_source
        CHECK (source IN ('OBSERVED_FLATTENED', 'OBSERVED_BOUNDARY')),
    -- 출처마다 채워질 수 있는 필드가 다르다는 사실을 스키마에 남긴다.
    -- OBSERVED_BOUNDARY 에 org_code 를 붙이려면 조직 동일성 판단이 필요하고,
    -- 그 판단은 시스템이 아니라 사람이 한다 (ADR-009).
    CONSTRAINT ministry_master_source_fields CHECK (
        (source = 'OBSERVED_FLATTENED' AND source_field = '소관부처명' AND org_code IS NOT NULL)
        OR (source = 'OBSERVED_BOUNDARY' AND source_field = '법령구분명' AND org_code IS NULL)
    ),
    CONSTRAINT ministry_master_valid_range
        CHECK (valid_until IS NULL OR valid_from < valid_until),
    CONSTRAINT ministry_master_known_range CHECK (known_from < known_until)
);

CREATE UNIQUE INDEX ministry_master_current_key
    ON ministry_master (org_code, org_name, valid_from)
    WHERE known_until = 'infinity' AND org_code IS NOT NULL;

CREATE UNIQUE INDEX ministry_master_current_boundary_key
    ON ministry_master (org_name, valid_from)
    WHERE known_until = 'infinity' AND org_code IS NULL;

CREATE INDEX ministry_master_code ON ministry_master (org_code, valid_from, valid_until);

CREATE TRIGGER ministry_master_immutable
    BEFORE UPDATE OR DELETE ON ministry_master
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- ministry_unresolved — 마스터에 없는 부처를 만난 기록
-- ---------------------------------------------------------------------------
-- append-only 다. 관측 횟수를 컬럼으로 두고 증가시키면 UPDATE 가 필요해지고,
-- 그 UPDATE 는 "한 번만 예외로" 열리는 첫 구멍이 된다. 횟수는 COUNT(*) 로 센다.
--
-- 검토 큐가 생기면 이 테이블이 그대로 큐 입력이 된다 — "이 부처명이 마스터에
-- 없습니다. 새 조직인가요, 표기 변형인가요?"는 사람이 답할 질문이다 (ADR-009).
CREATE TABLE ministry_unresolved (
    id            uuid PRIMARY KEY,
    observed_name text        NOT NULL,
    observed_code text,
    document_id   uuid REFERENCES regulation_document (id),
    load_run_id   uuid        NOT NULL,
    observed_at   timestamptz NOT NULL,
    reason        text        NOT NULL,

    CONSTRAINT ministry_unresolved_reason
        CHECK (reason IN ('CODE_NOT_IN_MASTER', 'NAME_MISMATCH', 'CODE_MISSING'))
);

CREATE INDEX ministry_unresolved_name ON ministry_unresolved (observed_name, observed_at);

-- ---------------------------------------------------------------------------
-- load_run — 적재 완료 표시. 매니페스트와 같은 발상이다
-- ---------------------------------------------------------------------------
-- 이 행의 **존재 자체가 적재 완료를 의미한다.** 마지막에 쓰므로, 중간에 죽으면
-- 행이 없고 불완전한 적재가 완전한 것으로 보이지 않는다.
--
-- 문서 단위로 커밋하므로 "문서 단위로는 완전, run 단위로는 미완료"인 상태가
-- 존재한다. 그 상태의 문서는 DB 에 있으면서 어느 load_run 에도 속하지 않는
-- 고아 문서이며, `find_orphan_documents()` 로 찾는다. 매니페스트의 고아 페이지는
-- 읽히지 않아 무해했지만 고아 문서는 질의에 잡히므로 다르다.
CREATE TABLE load_run (
    id                    uuid PRIMARY KEY,
    source_key            text        NOT NULL,
    source_run_id         text        NOT NULL,
    started_at            timestamptz NOT NULL,
    completed_at          timestamptz NOT NULL,

    documents_loaded      int         NOT NULL,

    -- 파싱된 조문단위 수와 처분(disposition)별 건수. 넷의 합이 parsed_units 와
    -- 같아야 한다. 한 단위는 정확히 하나의 처분을 받는다.
    --   loaded             적재됨, 소관부처가 마스터에서 해결됨
    --   loaded_unresolved  적재됨, 소관부처 미해결 — 적재에 흡수되어 사라지지 않게 분리
    --   skipped            이미 있어서 건너뜀 (멱등)
    --   key_conflicts      식별키 충돌. 발생하면 run 이 중단되므로 완료된 run 에서는 0이다.
    --                      그럼에도 컬럼을 두는 이유는 "검사했고 0이었다"를 기록하기
    --                      위해서다 — 검사가 돌지 않아서 0인 것과 구별된다
    parsed_units          int         NOT NULL,
    loaded                int         NOT NULL,
    loaded_unresolved     int         NOT NULL,
    skipped               int         NOT NULL,
    key_conflicts         int         NOT NULL,

    CONSTRAINT load_run_counts_partition CHECK (
        loaded + loaded_unresolved + skipped + key_conflicts = parsed_units
    ),
    CONSTRAINT load_run_time_range CHECK (started_at <= completed_at)
);

CREATE INDEX load_run_source ON load_run (source_key, completed_at);

COMMENT ON TABLE load_run IS
    '적재 완료 표시. 행의 존재가 곧 완료를 의미하므로 마지막에 쓴다 (스냅샷 매니페스트와 같은 발상)';
