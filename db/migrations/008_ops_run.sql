-- ---------------------------------------------------------------------------
-- 008. 일일 운영 실행 이력 — "언제부터 돌았고 며칠 실패했나"에 답하는 테이블
-- ---------------------------------------------------------------------------
--
-- `load_run` 이 이 역할을 하지 못하는 이유가 이 마이그레이션의 존재 이유다.
--
--   1. `load_run` 은 **스냅샷 하나의 적재 완료 표시**다. 행의 존재가 곧 완료를
--      의미하도록 설계했고(`completed_at NOT NULL`), 그래서 **실패한 실행은 행이
--      아예 없다.** "실패했다"와 "실행하지 않았다"가 같은 부재로 표현된다.
--   2. 하루 실행 하나가 `load_run` 을 여러 개 만든다 — 법령마다 본문 스냅샷이
--      하나씩이고, 직전 버전을 재수집하면 또 하나다. 실행 단위가 아니다.
--   3. 카나리아 결과, 폴링한 날짜, change_set 건수처럼 **적재 바깥의 사실**을
--      담을 자리가 없다.
--
-- 그래서 실행 단위 테이블을 따로 둔다. `load_run` 을 확장하지 않는 이유는 그
-- 테이블의 계약("행이 있으면 완료")을 깨지 않기 위해서다 — 실패 행을 허용하는
-- 순간 기존 조회(고아 문서 탐지)가 조용히 틀린다.
--
-- 이 두 테이블은 규제 데이터가 아니라 **운영 기록**이다. 그럼에도 UPDATE/DELETE 를
-- 막는 이유: 운영 실적은 "N일간 돌았고 K일 실패했다"를 주장하는 근거인데, 사후에
-- 고칠 수 있으면 주장이 아니라 희망이 된다.

-- ---------------------------------------------------------------------------
-- ops_run — 일일 실행 1건
-- ---------------------------------------------------------------------------
CREATE TABLE ops_run (
    id                  uuid PRIMARY KEY,

    -- 스냅샷 `run_id` 와 같은 값(UTC 타임스탬프 + 난수). 이 실행이 만든 스냅샷
    -- 디렉터리가 전부 이 접두사 아래에 있으므로, 실행 이력에서 원문으로 바로 간다.
    run_id              text        NOT NULL,

    started_at          timestamptz NOT NULL,
    finished_at         timestamptz NOT NULL,

    --   SUCCEEDED               새로 처리한 법령이 있고 실패가 없다
    --   SUCCEEDED_ZERO          카나리아·폴링은 정상인데 코퍼스 대상 새 개정이 0건이다.
    --                           **연속 0건 알람의 계수 대상**이며 실패가 아니다
    --   PARTIAL                 일부 날짜 또는 일부 법령이 실패했다. 나머지는 처리됐다
    --   FAILED                  폴링한 날짜가 전부 실패했다
    --   SKIPPED_CANARY_FAILED   카나리아 실패로 **수집을 하지 않았다.** 실패가 아니라 미수행이며
    --                           실패율 지표에 넣으면 우리 성능이 아닌 것을 우리 성능으로 계상한다
    status              text        NOT NULL,

    -- 폴링 대상 날짜. 법제처가 준 8자리를 그대로 둔다 — date 로 바꾸지 않는 이유는
    -- 이 값이 우리 달력이 아니라 **요청 파라미터**이기 때문이다 (ADR-005).
    lookback_days       int         NOT NULL,
    target_dates        text[]      NOT NULL,

    canary_passed       boolean     NOT NULL,
    canary_total_count  int,

    -- 날짜별 폴링 결과. [{"reg_date","status","total_count","matched","detail"}]
    -- 별도 테이블로 빼지 않은 이유: 이 값을 조인해서 집계할 질문이 아직 없다.
    -- 필요해지면 그때 테이블로 승격한다 — 지금 만들면 쓰이지 않는 스키마가 굳는다.
    date_probes         jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 코퍼스와 교집합인 법령 버전(MST) 수와 그 처분. 넷의 합이 detected 와 같아야
    -- 한다 — `load_run` 의 처분 분할 CHECK 와 같은 발상이다. 세지 않은 것이 있으면
    -- 여기서 드러난다.
    --
    -- **제정(no_previous)을 기처리(skipped)에 합치지 않는다.** 합치면 "비교할 것이
    -- 없었다"와 "이미 비교했다"가 한 숫자가 되고, 제정이 몇 건이었는지는 법령 행을
    -- 조인해야만 나온다. 조인이 필요한 사실은 조회되지 않는다.
    laws_detected       int         NOT NULL,
    laws_diffed         int         NOT NULL,
    laws_skipped        int         NOT NULL,
    laws_no_previous    int         NOT NULL,
    laws_failed         int         NOT NULL,

    change_sets_created int         NOT NULL,

    -- 변경된 조문 수(added+deleted+modified+editorial). 적재된 조문 행 수가 아니다 —
    -- 후자는 같은 법령을 재수집해도 늘어나므로 "포착 실적"으로 읽으면 부풀려진다.
    articles_changed    int         NOT NULL,

    -- 조용한 열화를 드러낸다. 세 번 재시도하고 성공한 날과 한 번에 성공한 날은
    -- 운영에서 다른 정보다 (`RequestStats` 와 같은 이유).
    requests            int         NOT NULL,
    retries             int         NOT NULL,

    detail              text        NOT NULL,

    CONSTRAINT ops_run_status CHECK (status IN (
        'SUCCEEDED', 'SUCCEEDED_ZERO', 'PARTIAL', 'FAILED', 'SKIPPED_CANARY_FAILED'
    )),
    CONSTRAINT ops_run_time_range CHECK (started_at <= finished_at),
    CONSTRAINT ops_run_lookback CHECK (lookback_days >= 1),
    CONSTRAINT ops_run_law_partition CHECK (
        laws_diffed + laws_skipped + laws_no_previous + laws_failed = laws_detected
    )
);

CREATE UNIQUE INDEX ops_run_run_id ON ops_run (run_id);
CREATE INDEX ops_run_started ON ops_run (started_at);

COMMENT ON TABLE ops_run IS
    '일일 운영 실행 1건. load_run 과 달리 실패한 실행도 행으로 남는다 — "실패한 날"과 "실행하지 않은 날"을 구별하는 것이 이 테이블의 존재 이유다';

COMMENT ON COLUMN ops_run.status IS
    'SKIPPED_CANARY_FAILED 는 실패가 아니라 미수행이다. SUCCEEDED_ZERO 는 코퍼스 대상 새 개정 0건이며 정상이다 — 코퍼스 12개월 실측이 개정일 14일/365일이므로 0건이 오히려 기본 상태다';

COMMENT ON COLUMN ops_run.articles_changed IS
    '변경된 조문 수(added+deleted+modified+editorial). 적재 행 수가 아니다 — 재수집으로 부풀지 않는 값이어야 운영 실적으로 인용할 수 있다';

-- ---------------------------------------------------------------------------
-- ops_law_outcome — 실행 × 법령 버전(MST) 1건
-- ---------------------------------------------------------------------------
-- 실패한 법령과 사유가 여기 남는다. **한 법령의 실패가 실행 전체를 실패로 만들지
-- 않는다**(하루치가 통째로 날아간다). 그 결정의 대가는 "부분 성공"이라는 상태이고,
-- 그 상태를 읽을 수 있게 하는 것이 이 테이블이다.
CREATE TABLE ops_law_outcome (
    id                    uuid PRIMARY KEY,
    ops_run_id            uuid    NOT NULL REFERENCES ops_run (id),

    -- 이 MST 를 어느 날짜 폴링에서 보았는가. 8자리 그대로다.
    reg_date              char(8) NOT NULL,
    law_id                text    NOT NULL,
    law_name              text,
    mst                   text    NOT NULL,

    --   DIFFED           change_set 을 새로 만들었다
    --   SKIPPED_DONE     이미 처리된 MST 다. 최근 N일 재확인의 정상 경로이며 실패가 아니다
    --   NO_PREVIOUS      제정본이라 비교 대상이 없다. 정상적인 개정 유형이다 (12개월 법령 단위 159건)
    --   FAILED           수집·적재·비교 중 실패. 사유가 failure_detail 에 있다
    status                text    NOT NULL,

    change_set_id         uuid    REFERENCES change_set (id),
    from_mst              text,
    mst_resolution_source text,
    change_ratio_exceeded boolean,
    articles_changed      int,

    failure_detail        text,

    CONSTRAINT ops_law_outcome_status CHECK (status IN (
        'DIFFED', 'SKIPPED_DONE', 'NO_PREVIOUS', 'FAILED'
    )),
    -- 실패에는 사유가 있어야 하고, 실패가 아닌데 사유가 있으면 안 된다. 한쪽만
    -- 걸면 "사유 없는 실패"나 "사유가 남은 성공"이 조용히 들어온다.
    CONSTRAINT ops_law_outcome_failure_detail CHECK (
        (status = 'FAILED') = (failure_detail IS NOT NULL)
    ),
    CONSTRAINT ops_law_outcome_diffed_has_change_set CHECK (
        status <> 'DIFFED' OR change_set_id IS NOT NULL
    )
);

CREATE INDEX ops_law_outcome_run ON ops_law_outcome (ops_run_id);
-- "이 MST 를 이미 처리했는가"를 묻는 경로. 최근 N일 재확인이 매 실행마다 탄다.
CREATE INDEX ops_law_outcome_mst ON ops_law_outcome (mst, status);

COMMENT ON TABLE ops_law_outcome IS
    '실행 × 법령 버전 1건. 한 법령의 실패가 실행 전체를 실패시키지 않는다는 결정의 기록면이다';

COMMENT ON COLUMN ops_law_outcome.status IS
    'SKIPPED_DONE 은 최근 N일 재확인이 이미 처리한 MST 를 다시 만난 정상 경로다. NO_PREVIOUS 는 제정본이며 실패가 아니다';

-- ---------------------------------------------------------------------------
-- 불변성 — 운영 실적을 사후에 고칠 수 없게 한다
-- ---------------------------------------------------------------------------
CREATE TRIGGER ops_run_immutable
    BEFORE UPDATE OR DELETE ON ops_run
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE TRIGGER ops_law_outcome_immutable
    BEFORE UPDATE OR DELETE ON ops_law_outcome
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- 권한 — 수집 role 이 실행 이력을 쓴다 (원칙 5)
-- ---------------------------------------------------------------------------
-- app_graph / app_review 는 003 의 ALTER DEFAULT PRIVILEGES 로 SELECT 를 이미
-- 받는다. app_dispatch 에는 주지 않는다 — 승인 레코드 외에는 보지 않는다.
GRANT SELECT, INSERT ON ops_run, ops_law_outcome TO app_ingest;
