# 08. 배포 고려사항

## 이 문서의 목적

로컬 개발 환경과 실제 운영 환경의 차이, 그리고 운영 환경에서만 필요한 통제를 정리한다.
docker-compose 로 뜨는 것과 사내 인프라에 올리는 것은 다른 일이며, 그 차이를 문서로
남기지 않으면 배포 시점에 즉석 결정이 쌓인다.

<!-- 작성 원칙: 이 문서는 Terraform 코드가 아니라 "무엇을 결정해야 하는가"의 목록이다.
     실제 인프라 코드는 확정 후 infra/ 에 들어간다. -->

---

## 1. 로컬과 운영의 차이

<!-- 무엇을 쓸 것인가:
     - docker-compose 의 Postgres 와 운영 관리형 DB의 버전·확장 차이
     - 로컬 init 스크립트(infra/postgres-init/)는 운영에서 실행되지 않는다.
       운영의 role 생성과 권한 부여는 누가 어떻게 하는가
     - 비밀값 주입 방식의 차이 (.env vs 비밀 관리 서비스) -->

---

## 2. DB 권한 구성 (원칙 5)

<!-- 무엇을 쓸 것인가:
     - 운영에서 필요한 role 목록과 각 role 의 권한
     - LLM 프로세스용 읽기 전용 role 이 실제로 쓰기를 못 하는지 확인하는 절차
     - 이 확인을 배포 후 자동으로 수행하는 방법 (security 테스트를 운영 대상으로 실행)
     - 권한 변경 승인 절차 -->

---

## 3. 프로세스 분리

<!-- 무엇을 쓸 것인가:
     - API / 그래프 실행기 / dispatch 워커를 어떻게 분리 배포하는가
     - 각 프로세스에 주입되는 자격증명이 다르다는 점과 그 강제 방법
     - dispatch 워커가 LLM 자격증명을 갖지 않아야 하는 이유 -->

---

## 4. 네트워크 경계

<!-- 무엇을 쓸 것인가:
     - 외부 호출이 허용되는 방향 (법령 API, 모델 공급자)
     - dispatch 워커의 외부 발송 경로
     - 각 경로에 대한 이그레스 제한 -->

---

## 5. 관측

<!-- 무엇을 쓸 것인가:
     - 반드시 알람이 걸려야 하는 항목 (킬 스위치 상태 변화, 인용 검증 실패율,
       읽기 전용 role 의 쓰기 시도, 승인 없는 발송 시도)
     - 로그에 남기면 안 되는 필드 (03-data-classification.md 와 연결)
     - 06-runbook.md 의 각 절차가 어떤 알람에서 시작되는지 대응시킨다 -->

---

## 5-1. 경로 이동 기록 — AWS 이관 예행연습

**2026-08-20.** 저장소를 `~/Documents/core/project/regchange-copilot` 에서
`~/dev/regchange-copilot` 으로 옮겼다.

### 왜 옮겼는가

macOS TCC 가 보호 디렉터리(`~/Documents`·`~/Desktop`·`~/Downloads`) 아래의 스크립트를
launchd 프로세스가 **실행**하는 것을 거부한다. 읽기는 되고 실행만 막힌다 (exit 126).

실측(2026-08-20):

| 시도 | 결과 |
|---|---|
| launchd → `/bin/ls <repo>/Makefile` | **OK** — 파일 읽기는 된다 |
| launchd → bash 스크립트가 `cd <repo>` | **OK** |
| launchd → `/bin/bash <repo>/scripts/....sh` | **DENIED** (`Operation not permitted`) |
| launchd → `WorkingDirectory=<repo>` | **DENIED** (`getcwd` 실패) |

**대안은 `/bin/bash` 에 전체 디스크 접근 권한을 주는 것이었고, 이동을 택했다.**
그 권한은 시스템 전역이라 다른 것에도 영향을 주고, 몇 달 뒤 왜 켜져 있는지 기억하지
못한다. 이동은 부작용이 없고 되돌릴 수 있다.

### 왜 이것이 이관 예행연습인가

AWS 이관의 본질은 **경로가 전부 바뀌는 것**이다. 저장소 루트, 스냅샷 루트, 로그 경로,
스케줄러 정의가 동시에 달라진다. 로컬에서 경로 하나를 바꿔 보면 **무엇이 경로에
묶여 있는지**가 드러난다 — 그 목록이 곧 이관 체크리스트다.

`SNAPSHOT_ROOT` 를 환경변수로 뺀 결정(ADR-014)이 여기서 회수된다.

### 경로에 묶여 있던 것 — 이동 전 조사

| 대상 | 절대 경로를 갖는가 | 조치 |
|---|---|---|
| 매니페스트의 `directory` | **아니오.** `run_id/target/key` 상대 경로 | 없음 — 루트만 바뀌면 따라온다 |
| `.env` 의 `SNAPSHOT_ROOT` | 비어 있음 (저장소 안 기본값 사용) | 없음 |
| launchd plist | **예.** 5곳 (실행 경로·WorkingDirectory·로그 2개) | `make ops-install` 재실행으로 재생성 |
| `.venv` | **예.** 내부 스크립트 shebang 이 절대 경로 | `uv sync` 재실행 필요 |
| `data/snapshots`, `data/ops-logs` | 저장소 안 상대 경로 | 함께 이동 |
| Postgres 데이터 | 도커 볼륨. 저장소 밖 | 영향 없음 |

**AWS 이관에서 다시 볼 목록이 이것이다.** 항목마다 "절대 경로를 갖는가"를 묻고,
갖는 것은 전부 설정으로 빼거나 배포 시 재생성한다.

### 이동 전 확인 — 지시서의 전제가 틀렸다

**작업 지시서는 "이동은 이미 끝났다, 확인과 복구만 하라"를 전제했으나, 실측하니 저장소는
옮겨지지 않은 상태였다.** `~/dev` 디렉터리 자체가 없었고, `git rev-parse --show-toplevel`,
`.venv` shebang, plist 가 전부 옛 경로를 가리키고 있었다.

전제를 확인하지 않고 아래 확인 항목을 그대로 돌렸다면 **전부 통과했을 것이다.** 경로가
바뀌지 않았으니 도는 것이 당연하다. 그리고 그 통과가 이 절에 "이동 후 확인 결과"로
기록되어, 7단계 AWS 이관 때 **틀린 근거**가 되었을 것이다.

이 프로젝트가 반복해서 경계해 온 형태다 — **통과하는 것과 검증하는 것은 다르다**
(0.5단계 `CREATE TABLE` 단언, `docs/incidents/silent-undercounting.md`). 검증은 그것이
실패할 수 있는 조건에 놓였을 때만 검증이다.

**AWS 이관 시**: 이관 확인 스크립트의 첫 단계는 "이관이 실제로 일어났는가"여야 한다.
대상 리소스가 존재하고, 옛 리소스를 가리키는 참조가 남아 있지 않음을 먼저 단언한다.

<!-- 부수 관측: 이동 전 `find ~ -maxdepth 3 -name regchange-copilot` 이 빈 결과를 냈고
     이를 TCC 순회 차단으로 오독했다. 실제로는 저장소가 depth 4 에 있어 탐색 범위 밖이었다.
     `find ~/Documents` 순회는 정상이다. TCC 문제는 아래 exit 126 실측이 근거이며,
     이 find 관측은 근거가 아니다. -->

### 이동 후 확인 결과

**2026-08-20 13:38 PDT 이동, 13:50 PDT 확인 완료 (12분).** 사전 조사·기준선 확보를
포함하면 약 30분.

| # | 확인 항목 | 결과 |
|---|---|---|
| 1 | `.venv` 복구 | **조치 필요** — `uv sync` 만으로는 부족했다 (아래) |
| 2 | 스냅샷 매니페스트 12건 | **통과** — 12/12, sha256 전건 일치 |
| 3 | 운영 이력 보존 | **통과** — `ops_run` 2행 그대로 |
| 4 | `make lint` / `typecheck` / `test` | **통과** — 675건 (`.venv` 재생성 후) |
| 5 | `ops history` / `summary` / `alerts` | **통과** |
| 6 | launchd 재설치 + kickstart | **통과** — `last exit code = 0` |
| 7 | 실행이 `ops_run` 에 기록 | **통과** — `20260820T204935Z-4ccb` |

#### 확인 2 — 매니페스트는 조사대로 영향이 없었다

이동 전후로 같은 스크립트를 돌려 대조했다. 12건 전부 `read_pages` 의 sha256 검증을
통과했고, **페이지 바이트 수까지 이동 전과 동일**했다. 매니페스트의 `directory` 가
`run_id/target/key` 상대 경로라는 조사 판단이 실측으로 확인됐다.

검증에는 `directory` 필드가 실제 디렉터리 위치와 일치하는지도 포함시켰다. 일치를
확인하지 않으면 "읽혔다"가 "매니페스트가 가리키는 곳을 읽었다"를 보장하지 않는다.

#### 확인 3 — 도커 볼륨은 경로가 아니라 **디렉터리 이름**에 묶여 있다

조사는 "Postgres 데이터는 저장소 밖이므로 영향 없음"이라고만 적었다. 맞지만 **이유가
불완전했다.**

볼륨 이름은 `regchange-copilot_regchange-pgdata` 이고, 앞의 `regchange-copilot` 은
compose 프로젝트명이다. 이동 시점의 `docker-compose.yml` 에는 `name:` 이 없었으므로
**프로젝트명이 디렉터리 basename 에서 나왔다.** 이번 이동은 basename 이 그대로여서
같은 볼륨에 붙었을 뿐이다.

> **이동이 아니라 이름 변경이었다면 compose 가 새 빈 볼륨을 만들고, 운영 이력은
> 조용히 사라졌을 것이다.** 에러 없이, "DB가 비어 있네"로만 드러난다.

**조치 완료 (2026-08-20)**: `docker-compose.yml` 에 `name: regchange-copilot` 을
명시해 결합을 끊었다. 다른 이름의 디렉터리에서 해석시켜 검증했다:

| 디렉터리 basename | `name:` | 해석된 볼륨 |
|---|---|---|
| `renamed-repo` | 있음 | `regchange-copilot_regchange-pgdata` — 유지 |
| `renamed-repo` | 없음 | `renamed-repo_regchange-pgdata` — **새 빈 볼륨** |

적용 후 `docker compose down && up -d` 로 재기동해 `ops_run` 3행 보존을 확인했다.

이것은 백업을 대신하지 않는다. 결합 하나를 끊었을 뿐이고, 사본은 여전히 하나다 —
아래 §6 의 백업 미결정 사항은 그대로 열려 있다.

#### 확인 1 — `uv sync` 만으로는 복구되지 않는다 (조사에서 놓친 것)

조사는 `.venv` 에 대해 "`uv sync` 재실행 필요"라고 적었다. **실측 결과 그것으로는
부족했다.**

`.venv` 를 지우지 않고 `uv sync` 를 돌린 결과:

| | `uv sync` 전 | `uv sync` 후 | `.venv` 재생성 후 |
|---|---|---|---|
| 옛 경로를 참조하는 파일 | 33 | **30** | 0 |
| `.venv/bin/regchange` | bad interpreter | OK | OK |
| `_editable_impl_*.pth` | 옛 `src` 경로 | 새 경로 | 새 경로 |
| `.venv/bin/pytest` · `mypy` | bad interpreter | **bad interpreter** | OK |
| `make lint` · `typecheck` | — | **실패** | 통과 |

`uv sync` 는 **로컬 편집 가능 패키지 하나만 재설치했다** (`Uninstalled 1 / Installed 1`).
나머지 77개 서드파티 패키지의 콘솔 스크립트는 건드리지 않으므로 shebang 이 옛 절대
경로로 남는다.

세 가지가 예상 밖이었다:

1. **`uv run` 이 옛 shebang 을 우회하지 못한다.** Makefile 이 `uv run pytest` 를 쓰므로
   무사할 것으로 보였으나, `uv run` 은 `.venv/bin/` 의 실행 파일을 그대로 spawn 한다.
   `uv run mypy` → `bad interpreter`, `uv run lint-imports` → `Failed to spawn`.
2. **`ruff` 만 살아남았다.** 독립 실행 바이너리라 shebang 이 없다. 같은 `.venv/bin/`
   안에서도 Python 콘솔 스크립트와 네이티브 바이너리의 운명이 갈린다.
3. **실패 메시지가 원인을 가린다.** `Failed to spawn: No such file or directory` 는
   도구가 없는 것처럼 읽히지만 실제로는 도구는 있고 그 **인터프리터**가 없다.

**결론: 경로 이동 후 `.venv` 는 재생성한다 (`rm -rf .venv && uv sync`).** 부분 복구를
시도하면 일부만 고쳐진 상태가 만들어지고, 그 상태는 `regchange` 는 돌지만 게이트는
깨지는 형태여서 **정상으로 오인하기 쉽다.**

#### 확인 6 — TCC 가 해소됐다. 그리고 이것이 최초 성공 실행이다

이동 전 launchd 는 `last exit code = (never exited)` 였다 — **한 번도 완주한 적이
없었다.** `data/ops-logs/launchd.err.log` 에 그 증거가 그대로 남아 있다:

```
shell-init: error retrieving current directory: getcwd: cannot access parent directories: Operation not permitted
/bin/bash: /Users/lucasmac/Documents/core/project/regchange-copilot/scripts/ops/daily_ingest.sh: Operation not permitted
```

이동·재설치 후 kickstart 결과는 `last exit code = 0`, 그리고 `ops_run` 에 행이 남았다
(`20260820T204935Z-4ccb`, `SUCCEEDED_ZERO`, 카나리아 `totalCnt=83`, 7일 폴링 전건 정상).
**err.log 는 갱신되지 않았다** — 새로 쓸 에러가 없었다는 뜻이다.

exit code 0 만으로는 판정하지 않았다. 스크립트가 조용히 중간에 끝나도 0 이 나올 수
있으므로, **`ops_run` 에 행이 남은 것을 성공의 근거로 삼는다.** 이것이 ADR-014 에서
"실적 주장의 근거는 로그가 아니라 질의"라고 적은 이유다.

#### AWS 이관 시 대조표

| 대상 | 이동 전 예상 | 실제 | AWS 이관 시 |
|---|---|---|---|
| 매니페스트 `directory` | 영향 없음 | **맞음.** 12/12 sha256 통과, 바이트 동일 | S3 프리픽스로. 키가 상대 경로이므로 버킷·프리픽스만 설정으로 뺀다 |
| `.venv` | `uv sync` | **틀림.** `uv sync` 는 로컬 패키지만 고친다. 30개 잔여, 게이트 실패 → **재생성 필요** | 컨테이너 이미지. 빌드 시 생성되므로 이 문제가 구조적으로 사라진다 |
| launchd plist | 재설치 | **맞음.** `make ops-install` 이 5곳 치환, exit 0 | EventBridge Scheduler + ECS Task. 경로가 태스크 정의로 이동 |
| Postgres 데이터 | 영향 없음 (저장소 밖) | **맞지만 이유가 불완전.** 볼륨명이 디렉터리 basename 에 묶여 있었다 → `name:` 명시로 해소 | RDS. 스토리지가 컴퓨트와 분리되어 이 결합이 사라진다. **이관 전 덤프 필수** |
| `data/snapshots` · `ops-logs` | 함께 이동 | **맞음** | S3. 로그는 CloudWatch Logs |
| `.env` `SNAPSHOT_ROOT` | 비어 있음 | **맞음.** 기본값 경로가 따라옴 | Secrets Manager / SSM Parameter Store |
| git 작업 트리 | (조사 안 함) | **영향 없음.** 미커밋 31건 보존, `.git` 안에 절대 경로 없음 | 이관 대상 아님 (원격에서 clone) |
| TCC | 해소 기대 | **해소.** `(never exited)` → exit 0 | 해당 없음 — macOS 고유 제약 |

**이관 절차에 반영할 것 세 가지:**

1. **선행 단언** — "이관이 실제로 일어났는가"를 먼저 확인한다. 옛 리소스를 가리키는
   참조가 0건임을 단언하고, 0 이 아니면 중단한다
   (`grep -rl "<옛 경로>" <대상>` 이 이번 이동에서 33 → 30 → 0 을 드러냈다).

   단, **탐색 도구 자체가 결과를 걸러낼 수 있다.** 이번에 `.venv` 와 `data/` 를 훑을 때
   저장소 루트에서 돌린 재귀 grep 은 0 건을 냈지만, 경로를 명시하면 나왔다 — 셸의 grep
   래퍼가 gitignore 를 반영해 두 디렉터리를 건너뛰었기 때문이다. **런타임과 데이터는
   대개 gitignore 대상이고, 이관에서 경로가 박히는 곳이 정확히 거기다.** 잔여 참조
   스캔은 대상 경로를 명시하고, 도구가 무엇을 건너뛰는지 확인한 뒤 0 을 믿는다.
2. **런타임은 복구하지 말고 재생성한다** — 부분 복구는 "일부만 고쳐진 상태"를 만들고
   그 상태가 정상으로 보인다.
3. **이력은 이관 전에 덤프한다** — 볼륨·인스턴스가 이름 하나에 묶여 있고, 끊어지면
   에러 없이 빈 상태가 된다.

---

## 6. 미결정 사항

<!-- 무엇을 쓸 것인가:
     - 아직 정하지 않은 것을 정직하게 나열한다
     - 각 항목에 "언제까지 정해야 하는가"와 "정하지 않으면 무엇이 막히는가"를 적는다 -->

### 백업 — 로컬 운영 기간의 미결정 사항

**현재 운영 데이터의 사본이 하나뿐이다.** 도커 볼륨(`regchange-pgdata`)이 사라지면
운영 이력이 전부 사라지며, 그것은 복구할 수 없는 자산이다
(`docs/incidents/test-truncated-operations-history.md`).

- **언제까지**: AWS 이관(7단계) 전까지. 로컬 운영이 길어질수록 잃을 것이 커진다
- **정하지 않으면**: 볼륨 손상·오조작 한 번에 운영 실적 전부가 사라진다
- **후보**: `ops_run`/`ops_law_outcome` 만 주기적으로 덤프해 저장소 밖에 두는 것.
  규제 원문은 재수집 가능하므로 우선순위가 아니다

<!-- 무엇을 쓸 것인가:
     - 아직 정하지 않은 것을 정직하게 나열한다
     - 각 항목에 "언제까지 정해야 하는가"와 "정하지 않으면 무엇이 막히는가"를 적는다 -->
