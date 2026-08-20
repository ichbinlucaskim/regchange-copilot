-- ---------------------------------------------------------------------------
-- 007. 비교 짝의 출처와 이상 신호 — R-21 해소가 만든 새 실패에 대한 방어
-- ---------------------------------------------------------------------------
--
-- R-21 이전에는 `from_mst`/`to_mst` 를 사람이 지정했으므로 "조용히 잘못된 비교"가
-- 성립하지 않았다. 자동 확보 경로(`oldAndNew`)가 생기면서 그 실패가 새로 생긴다:
--
--   - 두 단계 건너뛴 버전과 비교하면 중간 개정이 통째로 섞인다
--   - 다른 법령의 MST 를 받으면 전 조문이 ADDED/DELETED 로 나온다
--   - 예외도 경고도 없다. 결과가 나오고 그럴듯해 보인다
--
-- 앞의 두 가지 중 "다른 법령"과 "공포일자 역전"은 이미 `compute_change_set` 이
-- DiffError 로 막는다. 이 마이그레이션이 채우는 것은 **짝을 어떻게 골랐는가**와
-- **그 선택이 이상해 보이는가**이다.
--
-- 컬럼을 `change_set` 에 두는 이유: 짝 선택은 그 비교 1건의 성질이다. 별도 테이블로
-- 빼면 조인 없이는 "이 diff 가 어떤 근거로 만들어졌는가"를 볼 수 없고, 조인이 필요한
-- 사실은 조회되지 않는다.

-- ---------------------------------------------------------------------------
-- 짝 선택의 출처
-- ---------------------------------------------------------------------------
ALTER TABLE change_set
    ADD COLUMN mst_resolution_source text NOT NULL DEFAULT 'MANUAL',
    ADD COLUMN resolved_from_mst     text,
    ADD CONSTRAINT change_set_mst_resolution_source
        CHECK (mst_resolution_source IN ('RESOLVED', 'MANUAL', 'MISMATCH'));

COMMENT ON COLUMN change_set.mst_resolution_source IS
    'from_mst 를 어떻게 골랐는가. RESOLVED=oldAndNew 로 자동 확보 / MANUAL=사람이 지정(골든셋 재현·테스트) / MISMATCH=자동 확보값과 지정값이 다름. MISMATCH 는 실패가 아니라 기록이다 — 손으로 지정하는 경로를 없애지 않기로 했으므로 불일치가 정상적으로 발생할 수 있고, 그때 조용히 넘기지 않기 위해 값으로 남긴다';

COMMENT ON COLUMN change_set.resolved_from_mst IS
    'oldAndNew 가 알려준 직전 MST. MANUAL 이어도 대조에 성공했으면 채운다. MISMATCH 판정의 근거이며, 이 값과 실제 from_document 의 mst 를 비교하면 무엇이 어긋났는지 바로 보인다. 자동 확보를 시도하지 않았으면 NULL';

-- ---------------------------------------------------------------------------
-- from 문서를 재사용했는가, 다시 받았는가
-- ---------------------------------------------------------------------------
ALTER TABLE change_set
    ADD COLUMN from_document_source text,
    ADD COLUMN reuse_skip_reason    text,
    ADD CONSTRAINT change_set_from_document_source
        CHECK (from_document_source IS NULL
               OR from_document_source IN ('REUSED', 'REFETCHED'));

COMMENT ON COLUMN change_set.from_document_source IS
    '직전 버전 본문의 출처. REUSED=이미 적재된 문서를 그대로 씀 / REFETCHED=다시 수집함. 재사용 조건은 (1) 해당 MST 의 문서가 있고 (2) 그 행의 source_run_id/source_page_sha256 으로 스냅샷을 찾을 수 있고 (3) 스냅샷 실제 sha256 이 기록된 값과 일치할 때다. 셋 중 하나라도 아니면 재수집한다';

COMMENT ON COLUMN change_set.reuse_skip_reason IS
    '재사용하지 않고 다시 받은 이유. NO_DOCUMENT / NO_SNAPSHOT / SHA256_MISMATCH / NOT_ATTEMPTED. **SHA256_MISMATCH 는 재수집으로 끝낼 일이 아니다** — 저장된 파일이 변조되었거나 기록이 틀렸거나 둘 중 하나이며 어느 쪽이든 사건이다. 재수집은 하되 이 값이 남고 로그에 경고가 뜬다. docs/incidents/ 기록 후보다';

-- ---------------------------------------------------------------------------
-- 변경 규모 이상
-- ---------------------------------------------------------------------------
ALTER TABLE change_set
    ADD COLUMN change_ratio_exceeded boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN change_set.change_ratio_exceeded IS
    '변경 조문 비율이 임계값을 넘었는가. 전부개정이면 정상이므로 revision_kind 가 전부개정인 경우에는 판정하지 않는다(항상 false). true 이면서 전부개정이 아니면 **다른 법령을 비교했을 가능성**을 먼저 의심한다 — 그 경우 전 조문이 ADDED/DELETED 로 나오고 비율이 1.0 에 가깝다. 임계값과 근거는 regchange.store.changeset 의 CHANGE_RATIO_WARN 참조';
