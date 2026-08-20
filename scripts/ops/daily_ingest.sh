#!/usr/bin/env bash
#
# 일일 작업 래퍼 — launchd/cron 이 부르는 한 줄.
#
# 목적:
#   `regchange ops daily` 를 **스케줄러 환경에서** 안전하게 돌린다.
#
# 구현 이유:
#   launchd/cron 은 사용자 셸 환경을 상속하지 않는다. PATH 가 최소이고, 작업
#   디렉터리가 `/` 이며, `.env` 는 로딩되지 않는다. 첫날 실패의 대부분이 여기서
#   나온다. 그래서 이 스크립트가 하는 일은 셋뿐이다 —
#   **작업 디렉터리 고정 / PATH 보강 / 로그 파일 append**.
#
#   `.env` 로딩은 여기서 하지 않는다. `regchange` 가 파이썬 안에서 읽는다 —
#   셸에서 export 하면 수동 실행과 스케줄 실행의 환경이 갈리고, 그 차이가
#   재현되지 않는 실패를 만든다.
#
#   가상환경의 실행 파일을 직접 부른다(`.venv/bin/regchange`). `uv run` 은
#   의존성 해석을 매번 시도하며 네트워크에 닿을 수 있고, 스케줄 실행이 네트워크
#   상태에 의존하게 만들 이유가 없다. `.venv` 가 없으면 `uv run` 으로 떨어진다.
#
# 트레이드오프:
#   로그를 파일로 남긴다(월 단위 회전). 구조화 로그가 stdout 으로 나가므로 파일이
#   곧 그 사본이며, 회전·보존 정책은 없다. 실행 이력의 1차 기록은 `ops_run`
#   테이블이고 이 파일은 **프로세스가 죽어 행조차 못 남긴 경우의 유일한 흔적**이다.
#
# 엣지 케이스:
#   - 종료 코드를 그대로 전달한다. launchd 가 실패를 볼 수 있어야 한다.
#   - 인자는 그대로 넘긴다. `daily_ingest.sh --days 14` 가 동작한다.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

# launchd 의 기본 PATH 는 /usr/bin:/bin:/usr/sbin:/sbin 이다. Homebrew 경로를 앞에
# 붙인다 — Apple Silicon 은 /opt/homebrew, Intel 은 /usr/local 이다.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

LOG_DIR="${REGCHANGE_LOG_DIR:-${REPO_ROOT}/data/ops-logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/daily-$(date -u +%Y%m).log"

# 실행 파일 선택. 벽시계는 UTC 로 찍는다 — 기록은 전부 UTC 라는 규칙과 같다.
if [[ -x "${REPO_ROOT}/.venv/bin/regchange" ]]; then
  RUNNER=("${REPO_ROOT}/.venv/bin/regchange")
elif command -v uv >/dev/null 2>&1; then
  RUNNER=(uv run regchange)
else
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FATAL: .venv 도 uv 도 없다. make setup 을 먼저 돌린다" \
    >>"${LOG_FILE}"
  exit 127
fi

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) start  cwd=${REPO_ROOT}  runner=${RUNNER[*]} ==="
  "${RUNNER[@]}" ops daily "$@"
  status=$?
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) end    exit=${status} ==="
  exit "${status}"
} >>"${LOG_FILE}" 2>&1
