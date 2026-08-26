-- 013 — 킬 스위치 상태 (5단계)
--
-- 목적:
--   검색·LLM 호출·발송을 **재배포 없이** 멈출 수 있게 하고, 누가 언제 왜 멈췄는지를
--   행으로 남긴다.
--
-- 구현 이유:
--   **왜 환경변수가 아닌가.** 요건이 셋이었다 — (a) 재배포 없이 60초 안에 반영,
--   (b) 값 변경을 기록에 남길 것, (c) AWS 이관 시 `adapters/` 뒤로 갈 것.
--   환경변수는 (a)를 만족하지 못한다. `api/app.py` 는 장수 프로세스이며 밖에서
--   프로세스의 환경변수를 바꿀 방법이 없다 — "60초 내 반영"이 "재시작"이 된다.
--   그리고 (b)를 남길 지점이 없다. 파일+mtime 은 (a)는 되지만 (b)가 파일 밖에 있어야
--   하고, 그 둘은 어긋난다.
--
--   **왜 bitemporal 인가.** 스위치의 과거 상태가 감사 질문의 답이다 — "왜 그날 분석이
--   안 돌았나"에 "LLM_ENABLED 가 꺼져 있었다"로 답하려면 그날의 행이 있어야 한다.
--   UPDATE 로 값을 덮으면 그 답이 사라진다 (원칙 6). 켜고 끈 이력이 곧 운영 이력이다.
--
--   **`reason` 과 `changed_by` 를 NOT NULL 로 둔다.** 껐다는 사실만 남으면 나중에
--   "왜 껐지"를 모른다. 그리고 이유를 쓰게 하면 함부로 끄지 않는다 — **사람이 한 줄
--   쓰는 마찰이 방어가 된다.** `changed_by` 는 로컬 단독 운영에서는 값이 뻔하지만,
--   필드가 있어야 나중에 채워진다. 빈 문자열도 막는다(`btrim`) — NOT NULL 만 걸면
--   `''` 이 들어오고 그것은 없는 것과 같다.
--
--   **쓰기 권한을 어떤 서비스 role 에도 주지 않는다.** 소유자만 INSERT/UPDATE 한다.
--   이유는 방향이다 — 프롬프트 인젝션이 성립하더라도 **스위치를 켜는 경로가 없어야**
--   한다. 끄는 경로가 없는 것도 같은 이유다(서비스가 스스로 자기를 끄면 그 사실이
--   운영자의 결정으로 보인다). 스위치는 사람이 CLI 로 바꾼다.
--
-- 트레이드오프:
--   - 스위치 조회가 DB 의존이다. DB 가 죽으면 스위치를 읽을 수 없고, 그때는 **멈춘다**
--     (fail-closed). "모르면 안 한다"가 이 시스템의 기본값이며 `INSUFFICIENT_EVIDENCE`·
--     카나리아 실패와 같은 계열이다. 반대 방향(모르면 계속 한다)이 더 편해 보이는
--     순간이 오지만, 그 순간이 정확히 스위치가 필요한 순간이다.
--   - 조회에 캐시(최대 60초)가 붙으므로 반영이 즉시가 아니다. 즉시성이 필요하면
--     프로세스를 재시작한다 (런북). 캐시 무효화 경로는 **지금 만들지 않는다** —
--     실제로 60초가 문제가 되는지 아직 모른다.
--
-- 엣지 케이스:
--   - **행이 아예 없음**: 꺼짐으로 읽는다. 마이그레이션이 아무 스위치도 켜지 않으므로
--     기본 상태는 전부 꺼짐이다 (CLAUDE.md §6, fail-closed). 켜는 것은 운영자의 명시적
--     행위여야 한다.
--   - **같은 스위치를 두 번 끔**: 행이 하나 더 쌓인다. 막지 않는다 — 두 번째 이유가
--     첫 번째와 다를 수 있고, 그 문장이 이력의 값이다.
--   - **닫힌 행을 다시 UPDATE**: 트리거가 거부한다 (RC002).
--   - 이름 오타: CHECK 가 거부한다. 알 수 없는 이름을 넣을 수 있으면 그 스위치는
--     아무도 읽지 않으면서 켜져 있는 것처럼 보인다.

BEGIN;

CREATE TABLE kill_switch (
    id           uuid PRIMARY KEY,

    name         text        NOT NULL,
    enabled      boolean     NOT NULL,

    -- 누가 왜 바꿨는가. 둘 다 필수다 (위 「구현 이유」 참조).
    changed_by   text        NOT NULL,
    reason       text        NOT NULL,

    known_from   timestamptz NOT NULL,
    known_until  timestamptz NOT NULL DEFAULT 'infinity',

    CONSTRAINT kill_switch_known_range CHECK (known_from < known_until),
    CONSTRAINT kill_switch_name CHECK (
        name IN ('RETRIEVAL_ENABLED', 'LLM_ENABLED', 'DISPATCH_ENABLED')
    ),
    CONSTRAINT kill_switch_changed_by_present CHECK (btrim(changed_by) <> ''),
    CONSTRAINT kill_switch_reason_present CHECK (btrim(reason) <> '')
);

-- 현재 행(열린 행)은 스위치당 하나다. 닫힌 과거 행은 여러 개이며 그것이 이력이다.
CREATE UNIQUE INDEX kill_switch_current ON kill_switch (name) WHERE known_until = 'infinity';
CREATE INDEX kill_switch_known ON kill_switch (name, known_from, known_until);

COMMENT ON TABLE kill_switch IS
    '킬 스위치 상태와 그 변경 이력. 행이 없으면 꺼짐이다 (fail-closed)';
COMMENT ON COLUMN kill_switch.reason IS
    '왜 바꿨는가. 필수 — 껐다는 사실만 남으면 나중에 왜 껐는지를 모른다';
COMMENT ON COLUMN kill_switch.changed_by IS
    '누가 바꿨는가. 필수 — 로컬 단독 운영에서도 필드가 있어야 나중에 채워진다';

CREATE TRIGGER kill_switch_immutable
    BEFORE UPDATE OR DELETE ON kill_switch
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

-- ---------------------------------------------------------------------------
-- 권한 — 전부 읽기만. 쓰기는 소유자(사람)만.
-- ---------------------------------------------------------------------------
-- app_dispatch 에도 SELECT 를 준다. 003 은 이 role 의 시야를 action_outbox 로 좁혔지만,
-- **자기를 멈추는 스위치는 읽어야 멈출 수 있다.** 이 한 테이블은 승인 내용도 프롬프트도
-- 담지 않으므로 원칙 5 의 경계를 넓히지 않는다.
GRANT SELECT ON kill_switch TO app_graph, app_review, app_ingest, app_dispatch, app_policy;

COMMIT;
