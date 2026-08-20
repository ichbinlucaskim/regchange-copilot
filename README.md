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

## 운영 실적

> **(자리표시자)** 아직 운영 데이터 없음. 0단계(스캐폴딩) 상태다.

<!-- 채울 것: 감시 중인 법령 수, 포착한 개정 건수, 제안 건수, 승인/반려 비율,
     담당자 검토 소요 시간의 변화. baseline 없이 목표치만 적지 않는다. -->

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

`make setup` 이 만드는 `.env` 는 킬 스위치가 **전부 꺼진 상태**로 시작한다
(`LLM_ENABLED`, `RETRIEVAL_ENABLED`, `DISPATCH_ENABLED` = `false`). 기본값이 꺼짐인
이유는 설정 누락이나 신규 환경 기동이 조용히 기능을 활성화하는 일이 없어야 하기 때문이다.
켜는 것은 명시적 행위여야 한다.

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
├─ retrieval/     사내 규정 문단 검색
├─ graph/         LangGraph 그래프·승인 interrupt  [원칙 4]
├─ prompts/       프롬프트 정의 (생성기/검증기 분리) [원칙 3]
├─ verification/  인용 검증 — 생성과 분리된 경로    [원칙 2, 3]
├─ guards/        킬 스위치, 스키마 강제, 인용 폐기 [원칙 2, 5]
├─ api/           FastAPI 진입점
├─ dispatch/      승인 레코드만 보고 동작하는 발송 워커 [원칙 5]
└─ audit/         감사 로그와 재현
```

각 패키지의 역할·설계 이유·트레이드오프·엣지 케이스는 해당 `__init__.py` 의 docstring에
있다. 이 프로젝트는 모든 구성요소에 **4항목 docstring**(목적 / 구현 이유 / 트레이드오프 /
엣지 케이스)을 요구한다. 규칙은 [CLAUDE.md §3](./CLAUDE.md).

---

## 현재 단계

**1단계 완료 — 수집·파싱·적재·차분.** 각 항목은 실측 또는 픽스처 채점과 함께 들어왔다.

| 영역 | 상태 | 근거 |
|---|---|---|
| 법제처 API 호출 | 구현 | `ingest/`. 응답 형태 6종 분류, 완주 검사, 스냅샷 매니페스트 |
| 조문 파서 | 구현 | `parse/`. 픽스처 채점 정확도 1.0000 |
| DB 스키마·적재 | 구현 | `db/migrations/`, `store/`. bitemporal(원칙 6), 문서 단위 트랜잭션 |
| 결정론적 조문 diff | 구현 | `diff/`. 이동 후보 포함, I/O 의존 없음(원칙 1) |
| 도메인 선택 | 확정 | 12개월 전수 실측 — `docs/domain-selection/amendment-frequency.md`, ADR-008 |
| 평가 골든셋 | 시나리오 15건 | `evals/datasets/golden/`. 문서 본문은 2-B에서 작성 |

**아직 의도적으로 비어 있다**: 검색(`retrieval/`), LangGraph 노드·승인 interrupt(`graph/`),
프롬프트(`prompts/`), 인용 검증(`verification/`), 킬 스위치(`guards/`), API(`api/`),
발송 워커(`dispatch/`), 감사(`audit/`), Terraform. **검증 없이 쌓지 않는다** —
각 항목은 평가 데이터셋 또는 원문 확인과 함께 들어온다.

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
