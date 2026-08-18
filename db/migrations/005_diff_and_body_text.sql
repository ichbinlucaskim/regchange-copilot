-- 005 — 조문 전문 조립본과 diff 결과 테이블
--
-- 목적:
--   (1) 변경 감지가 쓸 조문 전문(조문내용 + 항 + 호 + 목) 조립본을 저장한다.
--   (2) `change_set` / `article_change` / `article_move_candidate` 를 실제 정의로
--       채운다. 002 의 정의는 권한 경계 검증용 최소 골격이었다.
--
-- 구현 이유:
--   **004까지의 `text_norm_sha256` 은 변경 감지에 쓸 수 없다.** 그것은 `조문내용`
--   하나만의 해시인데, `<항>` 이 있는 조문에서 `조문내용` 은 제목 줄뿐이다
--   (edge-case #5). 특금법 2011↔2020 실측에서 그 기준으로 비교하면 일치율 0.7407
--   (놓친 변경 5건)이고, 전문을 조립하면 0.9259(놓친 변경 0건)다. 제목만 같으면
--   본문이 통째로 바뀌어도 "변경 없음"이 된다 — 값이 채워지고 질의도 도는 형태의
--   조용한 실패다.
--
--   조립 규칙은 `regchange.parse.assemble.assemble_body()` 한 곳에만 둔다. 적재가
--   그 함수를 불러 컬럼을 채우고, diff 는 컬럼을 읽는다. 규칙이 두 곳에 있으면
--   검색된 텍스트와 diff 한 텍스트가 미묘하게 달라지고, **인용이 가리키는 것과
--   변경 판정의 대상이 어긋난다** — 원칙 2의 전제가 무너지며 예외는 나지 않는다.
--
--   `article_move_candidate.status` 에 `PENDING` 외의 값을 넣을 수 없게 CHECK 를
--   건다. ADR-003 의 "자동 확정하지 않는다"를 애플리케이션 조건문이 아니라 제약으로
--   강제한다. 확정 상태는 검토 기능(작업 5)이 마이그레이션과 함께 추가한다.
--
-- 트레이드오프:
--   조문당 텍스트를 한 벌 더 저장한다. `text_raw` 와 겹치는 부분이 있어 용량이
--   는다. 그 대신 비교 대상이 명시적인 컬럼이 되어, 다음 사람이 어느 컬럼으로
--   비교해야 하는지 추측하지 않는다.
--
--   기존 `text_norm_sha256` 을 지우지 않고 남긴다. 지우면 "조문내용만의 해시"라는
--   사실 자체가 사라져 같은 실수가 반복된다. 대신 COMMENT 에 **변경 감지에 쓸 수
--   없다**를 명시한다.
--
--   `change_set` 의 건수 CHECK 를 양방향으로 건다. 컬럼이 늘고 INSERT 가 길어지지만,
--   조문 개수 보존이 깨진 채로 커밋되는 경로가 없어진다. 이 저장소는 그 사고를
--   실제로 겪었다 — dict 붕괴로 `0013001` 이 소실돼 "벌칙 1:2"로 잘못 세었고 그것이
--   ADR-003 의 근거로 쓰였다 (silent-undercounting.md 사건 1).
--
-- 엣지 케이스:
--   - 기존 조문 행이 있는 상태로 적용: 조립본을 사후에 만들 수 없으므로(파서가
--     필요하다) 실패시킨다. 재적재가 정답이며, 조용히 빈 문자열로 채우지 않는다.
--   - 002 의 세 테이블에 데이터가 있는 경우: DROP 이 실패한다. 현재는 비어 있다.
--   - 같은 두 버전을 다시 diff: `UNIQUE (from_document_id, to_document_id)` 가
--     막는다. 중복 제거가 아니라 건너뛰기로 처리한다 (edge-case #18).

-- ---------------------------------------------------------------------------
-- 가드 — 조립본 없이 적재된 행이 남아 있으면 멈춘다
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    existing bigint;
BEGIN
    SELECT count(*) INTO existing FROM regulation_article;
    IF existing > 0 THEN
        RAISE EXCEPTION
            'regulation_article 에 % 행이 있다. 조립본(body_norm)은 파서 없이 만들 수 없으므로 '
            '빈 값으로 채우지 않는다. 데이터를 비우고 재적재한 뒤 다시 적용한다', existing
            USING ERRCODE = 'RC004';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 조문 전문 조립본
-- ---------------------------------------------------------------------------
ALTER TABLE regulation_article
    ADD COLUMN body_norm        text     NOT NULL,
    ADD COLUMN body_norm_sha256 char(64) NOT NULL,
    ADD COLUMN body_markers     jsonb    NOT NULL DEFAULT '[]'::jsonb,
    -- 이동 표기. 파싱 원문을 함께 보존한다 — `조문참고자료` 는 API 가 구조화해 준
    -- 필드가 아니라 우리가 자유 텍스트에서 추출한 것이므로 사후 검증이 가능해야
    -- 한다 (ADR-003, ADR-007 의 PARSED 와 같은 지위).
    ADD COLUMN reference_raw    text,
    ADD COLUMN moves            jsonb    NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN regulation_article.moves IS
    '조문참고자료에서 추출한 이동 표기. 각 항목은 날짜를 갖는다 — 이 조문의 이동 이력 전체가 누적되므로 diff 는 날짜 창으로 걸러 쓴다';
COMMENT ON COLUMN regulation_article.reference_raw IS
    '조문참고자료 원문. 파서가 놓친 표기를 사후에 찾을 수 있게 보존한다';

COMMENT ON COLUMN regulation_article.text_norm_sha256 IS
    '조문내용 하나만의 해시다. **변경 감지에 쓰지 말 것** — 항이 있는 조문에서 조문내용은 제목 줄뿐이라(edge-case #5) 본문 변경을 놓친다. 비교는 body_norm_sha256 으로 한다';
COMMENT ON COLUMN regulation_article.body_norm IS
    '조문내용 + 항 + 호 + 목을 문서 순서로 이은 정규화본. 조립 규칙은 parse.assemble.assemble_body() 하나뿐이다';
COMMENT ON COLUMN regulation_article.body_norm_sha256 IS
    '조립본의 sha256. MODIFIED 판정의 기준이다';
COMMENT ON COLUMN regulation_article.body_markers IS
    '전 계층에서 모은 개정 마커. body_norm_sha256 이 같은데 이 값이 다르면 EDITORIAL 이다';

CREATE INDEX regulation_article_body_hash ON regulation_article (body_norm_sha256);

-- ---------------------------------------------------------------------------
-- 제개정구분 — 타법개정 축 (본문 API 의 <기본정보><제개정구분>)
-- ---------------------------------------------------------------------------
ALTER TABLE regulation_document ADD COLUMN revision_kind text;
COMMENT ON COLUMN regulation_document.revision_kind IS
    '제정/일부개정/전부개정/타법개정. 법종구분(법률/대통령령)과 다른 축이며, 변경이력의 변경사유로는 타법개정을 식별할 수 없다(20,070건이 전부 조문변경)';

-- ---------------------------------------------------------------------------
-- diff 결과 테이블 — 002 의 최소 골격을 실제 정의로 교체한다
-- ---------------------------------------------------------------------------
DROP TABLE article_move_candidate;
DROP TABLE article_change;
DROP TABLE change_set;

CREATE TABLE change_set (
    id                    uuid PRIMARY KEY,
    law_id                text        NOT NULL,
    from_document_id      uuid        NOT NULL REFERENCES regulation_document (id),
    to_document_id        uuid        NOT NULL REFERENCES regulation_document (id),

    -- 타법개정 축. 우선순위 정책은 분석 계층이 정하며 여기서는 기록만 한다.
    revision_kind         text,

    -- 이동 표기 날짜 창. `조문참고자료` 는 그 조문의 이동 이력 **전체**를 누적하므로
    -- (실측: 표기 128건 중 문서 시행 연도와 같은 해 0건, 소득세법 한 문서에 4개 시점)
    -- 창 밖 표기를 걸러내지 않으면 2008년 이동이 오늘의 후보로 올라온다.
    from_promulgation_date date       NOT NULL,
    to_promulgation_date   date       NOT NULL,

    computed_at           timestamptz NOT NULL,

    -- 조문 개수 보존. 양방향으로 건다 — 한쪽만 걸면 반대쪽 누락이 통과한다.
    from_article_count    int         NOT NULL,
    to_article_count      int         NOT NULL,
    added                 int         NOT NULL,
    deleted               int         NOT NULL,
    modified              int         NOT NULL,
    editorial             int         NOT NULL,
    unchanged             int         NOT NULL,

    -- 이동 표기 처분. 걸러낸 건수를 남긴다 — 조용한 제외를 만들지 않는다.
    moves_in_window       int         NOT NULL,
    moves_out_of_window   int         NOT NULL,
    out_of_window_dates   jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 후보 풀 크기. 대형 법령에서 폭발하는지 감시한다.
    candidate_pool_size   int         NOT NULL,

    CONSTRAINT change_set_from_partition
        CHECK (deleted + modified + editorial + unchanged = from_article_count),
    CONSTRAINT change_set_to_partition
        CHECK (added + modified + editorial + unchanged = to_article_count),
    CONSTRAINT change_set_window CHECK (from_promulgation_date <= to_promulgation_date),
    CONSTRAINT change_set_distinct_versions CHECK (from_document_id <> to_document_id)
);

CREATE UNIQUE INDEX change_set_version_pair
    ON change_set (from_document_id, to_document_id);
CREATE INDEX change_set_law ON change_set (law_id, computed_at);

COMMENT ON TABLE change_set IS
    '두 버전 비교 1건. 건수 CHECK 가 조문 개수 보존을 강제한다 — 이 저장소는 그 보존이 깨져 ADR-003 의 근거가 틀렸던 사고를 겪었다';

CREATE TABLE article_change (
    id              uuid PRIMARY KEY,
    change_set_id   uuid NOT NULL REFERENCES change_set (id),
    change_type     text NOT NULL,

    from_article_id uuid REFERENCES regulation_article (id),
    to_article_id   uuid REFERENCES regulation_article (id),

    -- 조문 좌표. 구조로 저장하고 "제5조의2" 는 렌더링 결과다 (ADR-001).
    article_no      int  NOT NULL,
    branch_no       int  NOT NULL DEFAULT 0,

    -- EDITORIAL 을 버리지 않고 최하위로 내린다. 감사에서 "이 문구정비 개정은 왜
    -- 검토 안 했나"를 물으면 인지했고 등급을 매겼다고 답할 수 있어야 한다.
    -- **우선순위 정책 자체는 분석 계층이 정한다.** 여기 있는 것은 기계적 강등뿐이다.
    priority_rank   smallint NOT NULL,

    CONSTRAINT article_change_type
        CHECK (change_type IN ('ADDED', 'DELETED', 'MODIFIED', 'EDITORIAL')),
    -- 유형과 참조가 어긋나는 상태를 스키마가 거부한다.
    CONSTRAINT article_change_endpoints CHECK (
        (change_type = 'ADDED'    AND from_article_id IS NULL     AND to_article_id IS NOT NULL)
     OR (change_type = 'DELETED'  AND from_article_id IS NOT NULL AND to_article_id IS NULL)
     OR (change_type IN ('MODIFIED', 'EDITORIAL')
         AND from_article_id IS NOT NULL AND to_article_id IS NOT NULL)
    ),
    CONSTRAINT article_change_editorial_is_demoted
        CHECK ((change_type = 'EDITORIAL') = (priority_rank = 9))
);

CREATE UNIQUE INDEX article_change_unique
    ON article_change (change_set_id, change_type, article_no, branch_no);
CREATE INDEX article_change_priority ON article_change (change_set_id, priority_rank);

COMMENT ON COLUMN article_change.priority_rank IS
    '0=기본, 9=최하위(EDITORIAL). 우선순위 정책은 분석 계층이 정하며 이 컬럼은 기계적 강등만 표현한다';

CREATE TABLE article_move_candidate (
    id              uuid PRIMARY KEY,
    change_set_id   uuid NOT NULL REFERENCES change_set (id),

    from_article_no int  NOT NULL,
    from_branch_no  int  NOT NULL DEFAULT 0,
    to_article_no   int  NOT NULL,
    to_branch_no    int  NOT NULL DEFAULT 0,

    score           real NOT NULL,
    evidence_kind   text NOT NULL,
    -- 세 신호를 전부 남긴다. 점수만 남기면 검토자가 왜 그 점수인지 알 수 없고
    -- 검토가 형식화된다. EXPLICIT 은 파싱 원문(raw)을 함께 남긴다 (ADR-003).
    evidence        jsonb NOT NULL,
    cardinality     text NOT NULL,

    -- ADR-003: 자동 확정하지 않는다. 명시 표기가 있어도 마찬가지다.
    -- 확정 상태는 검토 기능(작업 5)이 CHECK 를 넓히며 추가한다.
    status          text NOT NULL DEFAULT 'PENDING',

    CONSTRAINT article_move_candidate_evidence
        CHECK (evidence_kind IN ('EXPLICIT', 'TITLE', 'SIMILARITY')),
    CONSTRAINT article_move_candidate_cardinality
        CHECK (cardinality IN ('1:1', '1:N', 'N:1', 'N:M')),
    CONSTRAINT article_move_candidate_status_is_pending
        CHECK (status = 'PENDING'),
    CONSTRAINT article_move_candidate_score CHECK (score >= 0 AND score <= 1),
    CONSTRAINT article_move_candidate_not_self
        CHECK ((from_article_no, from_branch_no) <> (to_article_no, to_branch_no))
);

CREATE UNIQUE INDEX article_move_candidate_unique
    ON article_move_candidate (
        change_set_id, from_article_no, from_branch_no, to_article_no, to_branch_no
    );
CREATE INDEX article_move_candidate_score
    ON article_move_candidate (change_set_id, score DESC);

COMMENT ON TABLE article_move_candidate IS
    '이동 후보. status 는 PENDING 만 허용한다 — 자동 확정 경로를 스키마가 거부한다 (ADR-003)';

-- ---------------------------------------------------------------------------
-- 권한 재부여 — DROP/CREATE 로 002 의 GRANT 가 사라졌다
-- ---------------------------------------------------------------------------
-- app_graph / app_review 는 003 의 DEFAULT PRIVILEGES 로 SELECT 를 자동 취득한다.
-- app_ingest 는 명시 부여가 필요하다. app_dispatch 에는 주지 않는다 — 발송 워커가
-- diff 결과를 볼 이유가 없다 (원칙 5).
GRANT SELECT, INSERT ON change_set, article_change, article_move_candidate TO app_ingest;
