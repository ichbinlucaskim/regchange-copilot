-- ---------------------------------------------------------------------------
-- 010. llm_invocation 확정 — 첫 LLM 호출 이전에 만든다
-- ---------------------------------------------------------------------------
--
-- 002 는 이 테이블을 "경계 검증용 최소 정의"(`model`, `note`)로 두었다. 지금 확정한다.
--
-- **첫 호출 전에 만드는 것이 요점이다.** ADR-013 이 LangGraph 를 쓰는 조건으로 이 기록을
-- 걸었다 — "프레임워크가 실제로 무엇을 보냈는지 우리가 항상 볼 수 있어야 한다". 나중에
-- 붙이면 초기 호출들의 이력이 없고, 그러면 그 조건이 거짓이 된다.
--
-- 목적:
--   모든 모델 호출에 대해 **무엇을 보냈고 무엇을 받았는지**를 남긴다. 6개월 뒤 감사에서
--   "왜 이렇게 판단했는가"를 물었을 때 답할 수 있는 유일한 근거다.
--
-- 구현 이유:
--   1. **`temperature` 와 `seed` 컬럼을 만들지 않는다.**
--
--      기획서 6장은 이 둘을 재현 근거로 상정했다. **그 전제가 API 사실과 어긋난다** —
--      대상 모델(`claude-sonnet-5`, `claude-opus-5`)은 `temperature`/`top_p`/`top_k`/`seed`
--      를 **거부한다(HTTP 400)**. 샘플링 파라미터가 API 표면에서 제거됐다.
--
--      항상 NULL 인 컬럼을 만들면 그것을 보고 "재현 파라미터가 기록돼 있다"고 읽게 된다.
--      없는 것을 위한 자리를 만들지 않는다. 경위는
--      `docs/incidents/llm-api-schema-drift.md` (미수 3).
--
--   2. **`request_params_json` 을 둔다.** 실제로 보낸 파라미터 **전부**를 그대로 담는다.
--      닫힌 컬럼 집합으로 두면 파라미터가 하나 생길 때마다 같은 일이 반복된다 —
--      `변경사유` 를 닫힌 enum 으로 두지 않기로 한 것과 같은 논리다. 앞으로 무엇이
--      생길지 모른다는 사실을 스키마가 인정해야 한다.
--
--   3. **`api_version` 을 둔다.** 같은 모델 ID 라도 API 버전이 바뀌면 응답 형태와 기본값이
--      바뀔 수 있다. 모델 ID 만으로는 "그때 무엇이 돌았는가"가 확정되지 않는다.
--
--   4. **원본 출력을 DB 에 넣지 않고 저장소 키만 둔다** (`s3_key_raw_output`).
--      모델 출력은 크고, 스냅샷을 파일로 두고 해시로 검증하는 기존 방식과 같은 취급이
--      맞다 (ADR-007). 해시(`raw_output_sha256`)는 DB 에 남으므로 파일이 바뀌면 드러난다.
--      로컬에서는 `adapters/storage` 의 로컬 구현이 같은 키 공간을 쓴다 — 컬럼 이름이
--      `s3_` 로 시작하는 것은 기획서 표기를 그대로 따른 것이고, 배포 형태에 대한 약속이
--      아니다.
--
--   5. **`retrieved_chunk_ids` 를 배열로 둔다.** 이 집합이 인용 검증의 정답 집합이다
--      (원칙 2). 별도 테이블로 정규화하지 않는 이유는 이 배열이 **호출 시점에 고정된
--      스냅샷**이기 때문이다 — 조인으로 재구성하면 나중에 문단이 늘었을 때 과거 호출의
--      허용 집합이 함께 넓어진다. 그것은 감사 근거의 파괴다.
--
-- 트레이드오프:
--   - **replay 의 성격이 바뀐다.** `temperature=0` / `seed` 고정으로 "같은 답이 나오는지"
--     를 확인하는 replay 는 이 API 표면에서 성립하지 않는다. 남는 것은 "같은 입력으로
--     지금 돌리면 무엇이 나오는가"이며, 모델 교체 시 회귀 검증에는 여전히 쓸 수 있지만
--     **의미가 다르다.** 이 사실을 문서에 적지 않으면 나중에 "replay 가 다른 답을 내는데
--     버그 아닌가"가 된다. → ADR-013 「정정 이력」, `docs/incidents/llm-api-schema-drift.md`.
--   - `request_params_json` 은 jsonb 이므로 스키마가 강제되지 않는다. 오타 난 키가 조용히
--     들어갈 수 있다. 닫힌 컬럼의 안전성을 포기한 대신 확장성을 얻었다. 대응은 기록하는
--     쪽을 한 곳으로 모으는 것이다 — 어댑터가 자기가 보낸 요청 본문에서 직접 만든다.
--   - 이 테이블은 bitemporal 이 아니다. 호출 기록은 **사건**이지 상태가 아니며, 정정될
--     일이 없다. 대신 UPDATE/DELETE 를 트리거로 막는다 — 사건 기록이 사후에 바뀌면
--     감사 근거가 아니라 주장이 된다.
--
-- 엣지 케이스:
--   - **호출이 실패한 경우에도 행을 남긴다.** `outcome` 이 그것을 구별한다. 실패한 호출이
--     기록되지 않으면 "실패한 날"과 "호출하지 않은 날"이 같은 부재가 된다 — 008 이
--     `ops_run` 을 만든 것과 같은 이유다.
--   - 스키마 위반으로 폐기된 응답: `outcome='SCHEMA_INVALID'`, 원본 출력은 그대로 저장한다.
--     폐기한 것을 지우면 왜 폐기했는지 확인할 수 없다.
--   - 재시도: 시도마다 별도 행이며 `attempt` 로 구분한다. 재시도를 한 행에 합치면
--     "재작성 비율"(ADR-013 의 신호 2)을 셀 수 없다.
--   - 토큰 수가 없는 실패(네트워크 오류): NULL 을 허용한다. 0 으로 채우면 비용 집계가
--     조용히 틀린다.
--   - `retrieved_chunk_ids` 가 빈 배열: 정상이다. 검색 0건은 gate 가 판정할 입력이며,
--     그 사실도 기록돼야 한다.

DROP TABLE IF EXISTS llm_invocation;

CREATE TABLE llm_invocation (
    id                    uuid PRIMARY KEY,
    invoked_at            timestamptz NOT NULL,

    -- 어떤 단계의 호출인가. 생성기와 검증기를 구별한다 (원칙 3).
    purpose               text        NOT NULL,
    attempt               int         NOT NULL DEFAULT 1,

    -- 프롬프트 — 원본을 해시로 고정한다. 템플릿이 바뀌면 해시가 바뀐다.
    prompt_template_id    text        NOT NULL,
    prompt_template_sha256 char(64)   NOT NULL,

    -- 모델과 요청. `temperature`/`seed` 컬럼은 의도적으로 없다 (위 구현 이유 1).
    model_id              text        NOT NULL,
    api_version           text        NOT NULL,
    request_params_json   jsonb       NOT NULL,

    -- 입력 — 어느 문서의 어느 버전을 보고 만든 출력인가
    input_document_versions_json jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- 이 호출이 인용을 허용받은 문단 집합. gate 2단의 대조 대상이다 (원칙 2).
    retrieved_chunk_ids   uuid[]      NOT NULL DEFAULT '{}',
    retrieval_mode        text,

    -- 출력 — 본문은 저장소에, 해시는 여기에 (ADR-007)
    raw_output_sha256     char(64),
    s3_key_raw_output     text,

    -- 비용과 지연. 실측값이며 추정치를 넣지 않는다.
    input_tokens                int,
    output_tokens               int,
    cache_read_input_tokens     int,
    cache_creation_input_tokens int,
    latency_ms                  int,

    -- 결과. 실패도 행으로 남는다.
    outcome               text        NOT NULL,
    error_detail          text,

    -- 지시 유도 패턴 탐지 결과 (기획서 10.1). 지금은 탐지·기록만 한다.
    injection_signals_json jsonb      NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT llm_invocation_outcome CHECK (
        outcome IN ('OK', 'SCHEMA_INVALID', 'REFUSAL', 'ERROR')
    ),
    CONSTRAINT llm_invocation_attempt_positive CHECK (attempt > 0),
    -- 성공했는데 출력이 없을 수 없다. 조용한 빈 성공을 막는다.
    CONSTRAINT llm_invocation_ok_has_output CHECK (
        outcome <> 'OK' OR (raw_output_sha256 IS NOT NULL AND s3_key_raw_output IS NOT NULL)
    )
);

CREATE INDEX llm_invocation_invoked_at ON llm_invocation (invoked_at);
CREATE INDEX llm_invocation_purpose ON llm_invocation (purpose, invoked_at);
CREATE INDEX llm_invocation_model ON llm_invocation (model_id, invoked_at);

COMMENT ON TABLE llm_invocation IS
    '모든 모델 호출 기록. 첫 호출 전에 만들었다 (ADR-013). 실패한 호출도 행으로 남는다';
COMMENT ON COLUMN llm_invocation.request_params_json IS
    '실제로 보낸 파라미터 전부. temperature/seed 컬럼이 없는 이유는 010 헤더 주석 참조';
COMMENT ON COLUMN llm_invocation.retrieved_chunk_ids IS
    '이 호출이 인용을 허용받은 문단 집합. 호출 시점 스냅샷이며 조인으로 재구성하지 않는다';
COMMENT ON COLUMN llm_invocation.api_version IS
    'Anthropic API 버전. 같은 모델 ID 라도 버전이 바뀌면 기본값과 응답 형태가 바뀔 수 있다';

-- 호출 기록은 사건이다. 사후에 고칠 수 있으면 근거가 아니라 주장이 된다.
CREATE OR REPLACE FUNCTION reject_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        '% 는 UPDATE/DELETE 할 수 없다. 호출 기록은 사건이며 정정되지 않는다', TG_TABLE_NAME;
END
$$;

COMMENT ON FUNCTION reject_event_mutation() IS
    '사건 기록 테이블의 UPDATE/DELETE 를 차단한다. bitemporal 정정 경로도 두지 않는다';

CREATE TRIGGER llm_invocation_immutable
    BEFORE UPDATE OR DELETE ON llm_invocation
    FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();

-- 003 의 GRANT 는 DROP 으로 사라졌다. app_graph 가 INSERT 할 수 있는 두 테이블 중 하나다.
GRANT SELECT, INSERT ON llm_invocation TO app_graph;
GRANT SELECT ON llm_invocation TO app_review;
