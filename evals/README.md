# evals

파이프라인 품질을 재현 가능한 형태로 측정하는 트리. 단위 테스트와 분리되어 있다
(이유: `runners/__init__.py` docstring 참조).

## 데이터셋 세 종류

| 디렉터리 | 무엇을 담는가 | 무엇을 검사하는가 |
|---|---|---|
| `datasets/golden/` | 정답이 확정된 개정 사례 | 정상 경로에서 올바른 문단을 올바른 근거로 제안하는가 |
| `datasets/adversarial/` | 오도하기 쉬운 사례 (유사 용어, 무관한 유사 조문, 프롬프트 인젝션 시도) | 그럴듯하지만 틀린 제안을 만들어내지 않는가 |
| `datasets/insufficient/` | 근거가 부족한 사례 | **"모른다" 또는 "영향 없음"을 말하는가.** 억지로 답을 만들면 실패다 |

`insufficient` 가 이 평가 체계의 핵심이다. golden 만 측정하면 항상 무언가를 답하는
시스템이 최고점을 받는다. 이 도메인에서는 근거 없는 제안이 놓친 제안보다 비싸다.

## 파서 채점 — 법령 XML을 정답지로 쓴다

행정규칙(`target=admrul`)은 조문 식별자를 **하나도** 주지 않는다. `<조문내용>` 202개가
평면 나열될 뿐이고 XML 속성이 0개다
(`docs/api-exploration/law-api-spec.md` §6.1). 따라서 텍스트 파서가 필요한데,
텍스트 파서는 정답이 없어 맞는지 채점할 수 없다.

**법령 XML로 정답지를 만든다.** 법령 응답에서 `조문내용`만 뽑아 이어 붙이면 행정규칙과
같은 평문 형태가 되고, **원래 트리(`조문키`, `조문가지번호`, 항/호/목)는 이미 알고 있으므로
그것이 정답지가 된다.** 텍스트 파서로 구조를 복원해 원래 트리와 대조한다.

- 입력: `tests/fixtures/law_api/law_*.xml`을 평문화한 것
- 정답: 같은 파일의 원래 조문 트리
- 어려운 케이스도 법령 쪽에서 정답을 가져올 수 있다 — 가지번호(제5조의2), 장/절/관 제목,
  부칙 경계, 제목 없는 삭제 조문(`제45조 <삭 제>`)
- 지표: 조문 경계 정확도, `article_path` 복원 정확도

이 방식 덕분에 행정규칙 파서는 착수 시점부터 검증 가능한 상태로 시작한다.
착수 조건과 근거는 `docs/api-exploration/edge-cases.md` D-3.

## 러너

| 러너 | 무엇을 재는가 | 규약 |
|---|---|---|
| `runners/retrieval_eval.py` | 검색 재현율·정밀도·DECOY 혼입률 | `docs/10-retrieval-evaluation-protocol.md` |
| `runners/obligation_eval.py` | 의무사항 추출과 gate 2단, 비용 | `docs/11-obligation-extraction-baseline.md` |
| `runners/delegation_sweep.py` | 위임 승격 `top_n` (R-22) | `docs/12-delegation-promotion-results.md` |
| `runners/impact_eval.py` | 영향평가·부서 배정·gate 3단·재작성률·비용 | `docs/12-impact-assessment-results.md` |

`impact_eval` 은 `--grounding anchored|de-anchored` 로 **gate 3단 검증기를 갈아 끼운다.**
기본값은 `anchored`(4단계 기준선)이며, 두 검증기를 같은 골든셋으로 돌려 대조한 결과가
같은 문서 §12 에 있다. **측정이 기본값을 바꾼다 — 그 반대가 아니다.**

뒤의 둘은 4단계에서 들어왔다. **검색 규약을 고치지 않는다** — `impact_eval` 도 k=10,
HYBRID, `as_of=2026-02-01`, KURE-v1 그대로이며 승격은 검색 파라미터가 아니라 후보 추가다.

## 채점 대상

| 지표 | 정의 | 상태 |
|---|---|---|
| 인용 정확도 | 인용된 문단 ID가 검색 결과에 실재한 비율 | **측정 중** — gate 2단이 강제하므로 100%여야 한다 |
| 인용 적합도 | 인용된 문단이 실제로 그 주장을 뒷받침한 비율 | **부분 측정** — gate 3단의 `SUPPORTED/PARTIAL/UNSUPPORTED` 분포가 기계 판정이고, 사람 판정은 6단계 |
| 기권율 | `insufficient` 집합에서 답을 만들어내지 않은 비율 | **측정 중** (EMPTY 3건) |
| 오탐율 | 영향이 없는 조문에 대해 제안을 만든 비율 | **측정 중** (NO_IMPACT 2건, decoy 인용 수) |

**인용 정확도와 적합도를 구별해서 본다** (ADR-013 엣지 케이스). ID가 실재하는데 그 문단이
주장을 뒷받침하지 않는 것이 정확히 F-6 이며, gate 2단은 그것을 잡지 못한다.

## 규칙

- 결과 파일(`results/`)은 커밋하지 않는다. 러너와 데이터셋으로부터 재생산되어야 한다.
- 실제 고객 데이터를 넣지 않는다.
- 확인하지 않은 조항 번호를 데이터셋에 넣지 않는다. `TODO(verify)` 로 둔다.


---

## 킬 스위치 (2026-08-21 이후)

**러너는 실제 스위치 상태를 읽는다** (ADR-019). `LLM_ENABLED` 나 `RETRIEVAL_ENABLED` 가
꺼져 있으면 러너가 **첫 케이스에서 멈춘다.**

```bash
regchange switch list
regchange switch on LLM_ENABLED --by <사람> --reason "<왜>"
```

스위치를 무시하도록 만들지 않았다. 측정이 스위치를 우회하면, **돈이 나가는 가장 비싼
경로가 정확히 스위치가 안 듣는 경로**가 된다.
