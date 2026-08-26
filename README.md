# RegChange Copilot

**법령 개정을 조문 단위로 포착해 사내 규정 영향을 근거와 함께 제안하고, 담당자 승인 없이는
어떤 실행도 일어나지 않는 규제 변경 관리 시스템**

<!-- 이미지 자리표시자.
     여기에 들어갈 것은 챗봇 대화 화면이 아니라 "감사 재현 화면"이다.
     특정 제안에 대해 (a) 어떤 조문 변경에서 출발했는지, (b) 어떤 사내 문단이 검색됐는지,
     (c) 인용이 그 검색 결과 안에 실재하는지, (d) 누가 언제 승인했는지를
     한 화면에서 되짚는 모습이어야 한다.
     이 시스템의 가치는 "대화가 된다"가 아니라 "6개월 뒤에 근거를 다시 보여줄 수 있다"에 있다.
     그 가치를 첫 이미지로 보여준다. -->

> **(자리표시자 — 이미지)** 감사 재현 화면 — 제안 하나의 근거 경로 전체

---

## 이 시스템이 하지 않는 일

1. 고객 거래 차단 여부를 판단하지 않는다.
2. STR(의심거래보고) 제출 여부를 결정하지 않는다.
3. 고객 위험등급을 자동으로 변경하지 않는다.
4. 정책 문서를 자동으로 수정하지 않는다. 제안만 하고 반영은 사람이 한다.
5. 외부로 자동 발송하지 않는다. 메일·메신저·티켓 생성 전부 승인 이후 별도 워커가 한다.
6. 법률 자문을 제공하지 않는다. 출력은 검토 보조 자료이지 법적 판단이 아니다.

이 목록은 미구현 기능이 아니라 **의도적인 범위 제외**다.

---

## 설계 결정 6가지

### 1. 조문 diff는 결정론적 코드로 처리한다

LLM은 "무엇이 바뀌었나"가 아니라 "그것이 무엇을 의미하나"에만 쓴다. 원문 두 버전이 모두
있는 상황에서 변경 탐지는 사실 확인이지 추론이 아니다. LLM을 넣으면 정확도는 떨어지고
비결정성만 늘어난다.

### 2. 인용은 출력 스키마의 필수 필드다

인용된 문단 ID가 실제 검색 결과에 없으면 **코드가 응답을 폐기한다.** 경고를 남기고
통과시키지 않는다. 담당자가 검증할 수 없는 문장을 그럴듯하게 내놓는 것이 이 도메인의
실패 모드다.

### 3. 초안 생성 노드와 인용 검증 노드를 분리한다

자기 초안을 자기가 검증하면 검증은 형식화된다. 검증기는 생성기의 프롬프트나 추론 과정을
보지 않고, 출력 텍스트와 검색된 원문만 받아 대조한다.

### 4. 인간 승인은 UI 검사가 아니라 LangGraph interrupt로 그래프 레벨에 둔다

UI 검사는 API를 직접 호출하면 우회된다. 그래프가 중단되어 있으면 **우회 경로 자체가
존재하지 않는다.** 테스트 전용 우회 플래그도 만들지 않는다.

### 5. LLM 프로세스는 DB 읽기 전용 role을 쓴다

쓰기와 발송은 별도 워커가 수행하며, 그 워커는 프롬프트나 모델 출력을 보지 않고 **승인
레코드만** 보고 동작한다. 프롬프트 인젝션이 성립해도 쓰기 권한이 없으면 피해가 조회로
제한된다. 이 경계는 애플리케이션 조건문이 아니라 **DB 권한**으로 강제한다.

### 6. bitemporal 모델(valid_time × transaction_time)을 쓴다

감사의 질문은 "지금 무엇이 맞는가"가 아니라 **"그 시점에 담당자가 알 수 있었던 정보는
무엇이었는가"**이다. 레코드를 덮어쓰면 이 질문에 답할 수 없다.

자세한 내용은 [CLAUDE.md](./CLAUDE.md), 아키텍처 결정은
[docs/05-architecture-decisions/](./docs/05-architecture-decisions/).

---

## 조정 가능한 지점

이 시스템에는 조정 가능한 지점이 **66개** 있다. 각각을 무엇으로 정했고 근거가 무엇인지를
[docs/17-engineering-knobs.md](./docs/17-engineering-knobs.md) 에 정리했다.

근거는 네 종류로 구별한다 — **측정**(대조 실험, 수치 있음, 13개), **추론**(근거는 있으나
재보지 않음, 17개), **관행**(표준값을 그대로, 12개), **설계**(값이 하나뿐이며 그것이 결정
자체, 24개). **조정 지점의 5분의 1만 실측으로 정해져 있다**는 것이 현재 상태다.

아래는 **측정으로 정한 것**만 추린 것이다.

| 층 | 노브 | 값 | 측정 근거 |
|---|---|---|---|
| 검색 | 임베딩 모델 | KURE-v1 | vs bge-m3, 재현율@10 **0.7667 vs 0.7167** (ADR-015) |
| 검색 | 결합 방식 | HYBRID (RRF) | 재현율 **동률**, tie-break(DECOY 혼입률)로 결정 (ADR-016) |
| 검색 | 위임 승격 `top_n` | 2 | 스윕 — N=1 은 회수 0에 후보만 +18, N=2 는 0.7667→**0.8167** (ADR-017) |
| 생성 | `max_tokens` | 16000 | 8000 에서 초안이 7,848 로 잘려 **거짓 근거부족 2건** 발생 |
| 검증 | `GroundingMode` | anchored | de-anchored 는 적중 **9→6**, 부서 재현율 **0.70→0.25**, 비용 +39% |
| 검증 | `MAX_REVISIONS` | 0 | 재작성이 **새 근거를 찾은 사례 0/15**. 대가 적중 1건, 이득 비용 **−21.4%** |

**결정론이 없다는 것이 ML 과 가장 다른 점이다.** `temperature`/`seed` 를 쓸 수 없으므로
노브 비교와 변동성을 가려야 한다. 3회 반복 실측 결과 **검색 경로는 편차 0**,
생성·판정 경로는 **집계값 편차 0~2 인데 케이스 단위로는 15건 중 10건이 흔들린다**
([docs/15-variability-results.md](./docs/15-variability-results.md)).
그래서 **집계값으로 노브를 비교하는 것은 유효하고, 케이스 단위로 비교하는 것은 유효하지
않다.**

---

## 운영 실적

**2026-08-20 운영 시작.** 매일 07:00 KST 에 자동 실행된다 (ADR-014).

```bash
regchange ops summary     # 운영 일수 / 성공·실패·미실행 일수 / 총 포착 건수
regchange ops history     # 실행 이력 — 실패한 법령과 사유까지
regchange ops alerts      # MISMATCH · 변경규모 초과 · 연속 0건 · 카나리아 실패
```

이 숫자는 로그 집계가 아니라 `ops_run` / `ops_law_outcome` 테이블 질의다. **실패한
실행도 행으로 남는다** — "실패한 날"과 "실행하지 않은 날"을 구별하는 것이 그 테이블의
존재 이유다. 실패를 숨긴 실적은 아무도 믿지 않는다.

<!-- 채울 것: N일 운영 / M건 포착 / 실패 K일 (원인별). 매월 `ops summary` 출력을 옮긴다.
     그 다음 단계에서: 제안 건수, 승인/반려 비율, 담당자 검토 소요 시간의 변화. -->

## 평가 지표

> **(자리표시자)** [docs/07-evaluation-report.md](./docs/07-evaluation-report.md)

<!-- 채울 것: 인용 정확도, 인용 적합도, 기권율, 오탐/미탐율.
     모든 숫자에 측정 일자·데이터셋 버전·모델 버전을 붙인다. -->

## 데모

> **(자리표시자)** 링크 없음.

---

## 로컬 실행

### 요구사항

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker

### 실행

```bash
make setup      # 의존성 설치 + .env 생성 (.env.example 복사)
make up         # Postgres + pgvector 기동 (localhost:5433)
make test       # 테스트
make check      # lint + typecheck + test
make down       # 컨테이너 종료
```

### 일일 운영

```bash
make ops-run        # 일일 작업을 지금 한 번 (cron 과 같은 경로)
make ops-install    # launchd 등록 — 매일 07:00 KST
make ops-summary    # 운영 실적
```

`SNAPSHOT_ROOT` 를 비워 두면 `data/snapshots` 를 쓴다. **운영에서는 절대 경로를 준다** —
스케줄러는 작업 디렉터리가 다르다. AWS 배포에서 이 값이 S3 프리픽스가 되며, 매니페스트의
`directory` 는 이미 루트 기준 상대 경로라 루트만 바꾸면 참조가 깨지지 않는다.

macOS 에서 저장소가 `~/Documents` 아래에 있으면 launchd 실행이 권한으로 막힌다.
`make ops-install` 이 감지해 경고하며, 해소 절차는
[docs/06-runbook.md §5-1](./docs/06-runbook.md).

**킬 스위치는 `.env` 가 아니라 DB 에 있다** (ADR-019). 새 환경은 세 스위치가 **전부 꺼진
상태**로 시작한다 — `kill_switch` 에 행이 없으면 꺼진 것으로 읽는다. 기본값이 꺼짐인
이유는 설정 누락이나 신규 환경 기동이 조용히 기능을 활성화하는 일이 없어야 하기 때문이다.

```bash
regchange switch list
regchange switch on LLM_ENABLED --by <사람> --reason "<왜 켜는가>"
```

`--by` 와 `--reason` 은 필수다. 반영은 **최대 60초**이며, 즉시 필요하면 프로세스를
재시작한다. 환경변수를 쓰지 않은 이유는 장수 프로세스를 재배포 없이 멈출 수 없고
"누가 언제 왜"를 남길 지점이 없기 때문이다.

### DB 접속 확인

```bash
docker compose exec postgres psql -U regchange -d regchange -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

읽기 전용 role(`regchange_llm_ro`)도 함께 생성된다. 이 계정으로는 테이블을 만들거나
쓸 수 없으며, 그 사실을 `tests/security/` 가 검사한다.

---

## 저장소 구조

```
src/regchange/
├─ adapters/      외부 시스템 경계 (storage / queue / llm / secrets)
├─ ingest/        법령 원문 수집·정규화
├─ diff/          조문 단위 결정론적 차분          [원칙 1]
├─ temporal/      bitemporal 모델과 시점 재구성    [원칙 6]
├─ retrieval/     사내 규정 문단 검색 + 위임 승격   [ADR-016, ADR-017]
├─ graph/         LangGraph 그래프·승인 interrupt  [원칙 4, ADR-018]
├─ prompts/       프롬프트 정의 (생성기/검증기 분리) [원칙 3]
├─ verification/  인용 검증 — 생성과 분리된 경로    [원칙 2, 3]
├─ guards/        킬 스위치, 스키마 강제, 인용 폐기 [원칙 2, 5]
├─ review/        검토 큐·승인 레코드·반려 사유      [원칙 4, ADR-018]
├─ api/           FastAPI 진입점 (검토 UI)
├─ dispatch/      승인 레코드만 보고 동작하는 발송 워커 [원칙 5]
├─ ops/           일일 작업·실행 이력·조회 명령       [ADR-014]
└─ audit/         감사 로그와 재현
```

**이 시스템은 에이전트가 아니라 워크플로다.** LLM 과 도구가 미리 정해진 코드 경로로
오케스트레이션되고, 조건 분기도 전부 코드가 판정한다. 구별과 근거는
[ADR-013 「용어」 절](./docs/05-architecture-decisions/adr-013-agentic-pattern-selection.md).

각 패키지의 역할·설계 이유·트레이드오프·엣지 케이스는 해당 `__init__.py` 의 docstring에
있다. 이 프로젝트는 모든 구성요소에 **4항목 docstring**(목적 / 구현 이유 / 트레이드오프 /
엣지 케이스)을 요구한다. 규칙은 [CLAUDE.md §3](./CLAUDE.md).

---

## 현재 단계

**4단계 완료 — 영향평가와 승인 게이트까지.** 각 항목은 실측 또는 픽스처 채점과 함께
들어왔고, **예고대로 되지 않은 것은 그렇게 기록했다**
(`docs/12-impact-assessment-results.md`).

| 영역 | 상태 | 근거 |
|---|---|---|
| 법제처 API 호출 | 구현 | `ingest/`. 응답 형태 6종 분류, 완주 검사, 스냅샷 매니페스트 |
| 조문 파서 | 구현 | `parse/`. 픽스처 채점 정확도 1.0000 |
| DB 스키마·적재 | 구현 | `db/migrations/`, `store/`. bitemporal(원칙 6), 문서 단위 트랜잭션 |
| 결정론적 조문 diff | 구현 | `diff/`. 이동 후보 포함, I/O 의존 없음(원칙 1) |
| 도메인 선택 | 확정 | 12개월 전수 실측 — `docs/domain-selection/amendment-frequency.md`, ADR-008 |
| 평가 골든셋 | 시나리오 15건 | `evals/datasets/golden/`. 문서 본문은 2-B에서 작성 |
| 일일 운영 | 가동 중 | `ops/`. launchd 매일 07:00 KST, 최근 7일 재확인, 실행 이력 테이블 (ADR-014) |

| 사내 규정 검색 | 구현 | `retrieval/`. 하이브리드(ADR-016), 로컬 임베딩(ADR-015). 재현율@10 **0.7667** |
| 위임 승격 (R-22) | 구현 | `retrieval/promote.py`, ADR-017. 재현율@k **0.8167**, R-22 3항목 중 1항목 회수 |
| 의무사항 추출 + gate 2단 | 구현 | `prompts/`, `guards/`. 인용을 코드가 대조해 폐기한다(원칙 2) |
| 영향평가 + gate 3단 | 구현 | `pipeline/impact.py`, `verification/`. IMPACT 근거 확보 **9/10** |
| 승인 게이트 | 구현 | `graph/`, `review/`, `api/`. **세 겹**(그래프 구조·DB 권한·FK), ADR-018 |
| LLM 호출 기록 | 구현 | `audit/invocation.py`. 첫 호출부터, 실패·재시도·재작성까지 행으로 |
| 신뢰 등급 · 스캔 범위 (R-23) | 구현 | `guards/trust.py`, 마이그레이션 012. 스캐너가 **타입으로** 외부 텍스트만 받는다 |
| 킬 스위치 3종 | 구현 | `guards/killswitch.py`, 마이그레이션 013, ADR-019. DB 에 상태, 못 읽으면 멈춘다 |

**아직 의도적으로 비어 있다**: 발송 워커(`dispatch/`)와 그 경로의 `DISPATCH_ENABLED`
검증(R-25), 인젝션 **차단** 로직, 사내 부서 마스터(R-24), 적대적 평가 세트, 결재선,
Terraform.
**검증 없이 쌓지 않는다** — 각 항목은 평가 데이터셋 또는 원문 확인과 함께 들어온다.

### R-21 해소 (2026-08-19) — 자동 diff 경로가 열렸다

**`lsJoHstInf` 폴링이 준 새 MST 하나로 diff까지 간다.** 직전 MST는
`lawService.do?target=oldAndNew` 의 `구조문_기본정보/법령일련번호` 가 준다.

```bash
regchange law previous --mst 285199   # 직전 MST 조회만
regchange diff auto     --mst 285199  # 직전 MST 를 스스로 찾아 diff
regchange diff manual --from-document-id ... --to-document-id ...  # 골든셋 재현
```

**수동 지정 경로를 없애지 않았다.** 두 경로가 같은 `compute_change_set` 을 통과하고,
어느 쪽으로 왔는지는 `change_set.mst_resolution_source`(RESOLVED / MANUAL / MISMATCH)로
구별된다.

**해소가 위험을 없앤 것이 아니라 종류를 바꿨다.** 직전 MST를 잘못 고르면 diff가 조용히
틀린 결과를 낸다 — 예외도 경고도 없고 결과는 그럴듯하다. 탐지 네 장치(법령ID 일치 /
공포일자 순서 / 연속성 / 변경 규모)를 함께 넣었고, 전부 발화시키는 테스트가
`tests/integration/test_autodiff_detection.py` 에 있다. 상세는 R-21.

**남은 미확인**: 표본 2건이 같은 법령·같은 해다. 시행령·전부개정·`현행여부=Y` 에서도
같은지는 미확인이며, 운영 중 `mst_resolution_source` 분포로 드러난다.
