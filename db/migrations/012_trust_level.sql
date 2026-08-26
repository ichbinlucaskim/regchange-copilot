-- 012 — 문서 신뢰 등급 (R-23 ①)
--
-- 목적:
--   문서가 **어디서 왔는가**를 스키마에 못박는다. 법령 원문은 외부에서 왔고(untrusted),
--   사내 규정은 우리가 썼다(trusted). 이 구별 위에서만 "무엇을 인젝션 스캔할 것인가"를
--   정할 수 있다.
--
-- 구현 이유:
--   R-23 의 관측은 "스캐너 감도가 높다"가 아니었다. 사내 규정 제20조의 "비밀번호"라는
--   정상 단어가 `TOOL_OR_KEY_SOLICITATION` 을 발화시켰고, 그것은 **사내 문서를 스캔
--   대상에 넣었기 때문**이다. 감도를 낮추면 이번엔 외부 텍스트의 진짜 신호를 놓친다.
--   두 등급을 한 임계값으로 다루는 한 어느 방향으로 조정해도 반대쪽이 나빠진다.
--
--   **코드가 등급을 어긴 것이 아니라 지킬 대상이 없었다.** 기획서 6장은 문서에
--   `trust_level` 을 두지만 이 저장소의 스키마에는 그 컬럼이 없었다. 그래서 조치의
--   1번이 컬럼이다 — 코드만 고치면 다음에 또 같은 자리에서 샌다.
--
--   **CHECK 를 한 값으로 고정한다.** `regulation_document` 는 `'untrusted'` 만,
--   `policy_document` 는 `'trusted'` 만 허용한다. 열린 값 목록으로 두면 적재 코드가
--   등급을 "고를 수 있게" 되고, 고를 수 있는 것은 틀릴 수 있다. 이 저장소는 같은
--   판단을 이미 두 번 했다 — `mst_resolution_source` 를 호출부가 주장하지 못하게
--   파생시킨 것(007), 조문 `valid_from` 을 CHECK 로 막은 것(001).
--
--   등급은 **문서의 종류로 결정되며 행마다 다르지 않다.** 그래서 컬럼이 값을 나르는
--   것이 아니라 **제약이 사실을 선언한다.** 첨부 문서처럼 새 유입 경로가 생기면 그때
--   새 테이블과 ADR 로 다룬다. 지금 값을 열어 두면 아무도 검증하지 않는 값이 된다.
--
-- 트레이드오프:
--   - 컬럼 하나가 늘고 값이 항상 같다. 저장 관점에서는 낭비다. 그 대가로 **질의가
--     등급을 물을 수 있게** 된다 — "지금 스캔 대상이 몇 건인가"를 테이블 이름이 아니라
--     데이터로 셀 수 있다. 테이블 이름으로 등급을 아는 코드는 테이블이 늘 때 조용히 샌다.
--   - `'untrusted'` 인 사내 문서를 표현할 수 없다. 그것이 의도다. 표현할 수 있게 되는
--     순간 "이 사내 문서는 왜 untrusted 인가"를 아무도 검토하지 않고 넣을 수 있다.
--
-- 엣지 케이스:
--   - 기존 행: DEFAULT 로 채워진다. 두 테이블 모두 등급이 종류로 결정되므로 소급
--     판정이 필요 없다 — 과거 행도 같은 값이다.
--   - 불변 트리거: `ALTER TABLE ... ADD COLUMN` 은 행 단위 UPDATE 트리거를 발화시키지
--     않는다. 기존 이력은 그대로 있고 known_until 도 건드리지 않는다.
--   - `classification` 과의 혼동: 아래 COMMENT 가 구별을 적는다. 둘은 직교한다.

BEGIN;

-- ---------------------------------------------------------------------------
-- regulation_document — 외부에서 온 문서
-- ---------------------------------------------------------------------------
ALTER TABLE regulation_document
    ADD COLUMN trust_level text NOT NULL DEFAULT 'untrusted';

-- `'trusted'` 가 들어갈 수 없다. 법령 원문은 법제처 API 응답이며 그 안에 무엇이
-- 들어 있을지 우리가 정하지 않는다 — 관보 원문이라 위험이 낮다는 것은 **지금의
-- 관측**이지 보장이 아니다.
ALTER TABLE regulation_document
    ADD CONSTRAINT regulation_document_trust_level
    CHECK (trust_level = 'untrusted');

COMMENT ON COLUMN regulation_document.trust_level IS
    '출처 신뢰 등급. 항상 untrusted — 외부 API 응답이다. 이 텍스트만 인젝션 스캔 대상이다 (R-23)';

-- ---------------------------------------------------------------------------
-- policy_document — 우리가 쓴 문서
-- ---------------------------------------------------------------------------
ALTER TABLE policy_document
    ADD COLUMN trust_level text NOT NULL DEFAULT 'trusted';

ALTER TABLE policy_document
    ADD CONSTRAINT policy_document_trust_level
    CHECK (trust_level = 'trusted');

COMMENT ON COLUMN policy_document.trust_level IS
    '출처 신뢰 등급. 항상 trusted — 사내 규정 원문이다. 인젝션 스캔 대상이 아니다 (R-23)';

-- ---------------------------------------------------------------------------
-- trust_level 과 classification 은 다른 축이다
-- ---------------------------------------------------------------------------
-- 이 둘을 섞으면 "CONFIDENTIAL 이니까 위험하다 → 스캔하자"는 잘못된 추론이 성립한다.
-- 민감도가 높은 사내 문서일수록 우리가 쓴 문서이며, 그것을 지시로 오인할 이유는 없다.
COMMENT ON COLUMN policy_document.classification IS
    '내용 민감도(INTERNAL 등) — 누가 볼 수 있는가. trust_level(출처 신뢰)과 직교한다: '
    'CONFIDENTIAL 사내 문서도 trusted 이고, 공개된 관보 원문도 untrusted 다';

COMMIT;
