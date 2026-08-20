#!/usr/bin/env bash
#
# launchd 등록 — 매일 07:00 KST.
#
# 목적:
#   템플릿의 `__REPO_ROOT__` 를 치환해 `~/Library/LaunchAgents` 에 설치하고
#   부트스트랩한다. 되돌리기(`--uninstall`)도 같은 스크립트가 한다.
#
# 구현 이유:
#   **cron 이 아니라 launchd 를 쓴다.** 이 시스템은 노트북에서 돈다. cron 은
#   예약 시각에 기계가 자고 있으면 그 실행을 **건너뛰고 알리지도 않는다.**
#   launchd 의 `StartCalendarInterval` 은 깨어난 직후 밀린 실행을 한 번 돌린다.
#   운영 실적이 "며칠 돌았나"인 이상 이 차이가 곧 실적의 차이다.
#
#   cron 이 나은 점(Linux 와 같은 문법, AWS 이관 시 그대로)은 실익이 없다 —
#   배포하면 스케줄러는 EventBridge 이고 어차피 다시 쓴다. 이관되는 것은
#   스케줄러 설정이 아니라 **한 줄 명령**(`regchange ops daily`)이다.
#
# 트레이드오프:
#   기계가 꺼져 있던 구간은 launchd 도 회수하지 못한다(부팅 후 1회만 돈다).
#   그 회수는 `--days` 재확인 창이 담당하며, 창을 넘겨 빈 구간은 `ops summary` 의
#   미실행 일수로 남는다. 숨기지 않는다.
#
# 엣지 케이스:
#   - 이미 등록되어 있으면 bootout 후 다시 bootstrap 한다(멱등).
#   - `--uninstall` 은 plist 를 지우고 등록을 해제한다.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.regchange.daily"
TEMPLATE="${REPO_ROOT}/infra/launchd/${LABEL}.plist.template"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET="${TARGET_DIR}/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

uninstall() {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "${TARGET}"
  echo "등록 해제: ${LABEL}"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
  exit 0
fi

[[ -f "${TEMPLATE}" ]] || {
  echo "템플릿이 없다: ${TEMPLATE}" >&2
  exit 1
}

mkdir -p "${TARGET_DIR}" "${REPO_ROOT}/data/ops-logs"

# 07:00 KST 를 이 기계의 로컬 시각으로 환산한다. launchd 의 StartCalendarInterval 에는
# 타임존 옵션이 없어 값이 언제나 로컬로 해석되기 때문이다. 기계가 KST 면 그대로 7 이다.
read -r HOUR MINUTE LOCAL_LABEL <<<"$(python3 - <<'PY'
import datetime as dt

KST = dt.timezone(dt.timedelta(hours=9))
target = dt.datetime.now(KST).replace(hour=7, minute=0, second=0, microsecond=0)
local = target.astimezone()
print(local.hour, local.minute, local.strftime("%H:%M %Z"))
PY
)"

sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__HOUR__|${HOUR}|g" \
    -e "s|__MINUTE__|${MINUTE}|g" \
    "${TEMPLATE}" >"${TARGET}"
chmod +x "${REPO_ROOT}/scripts/ops/daily_ingest.sh"

# 이미 등록되어 있으면 새 plist 가 반영되지 않는다. 먼저 내리고 올린다.
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${TARGET}"
launchctl enable "${DOMAIN}/${LABEL}"

# macOS TCC — ~/Documents · ~/Desktop · ~/Downloads 는 보호 디렉터리다. launchd 가
# 띄운 프로세스는 앱과 달리 권한이 상속되지 않아, 그 아래의 스크립트를 **실행**하려
# 하면 "Operation not permitted"(exit 126) 가 난다. 읽기는 되는데 실행이 막히므로
# 원인이 보이지 않는다 — 실측으로 확인한 동작이며, 여기서 미리 경고한다.
case "${REPO_ROOT}" in
  "${HOME}/Documents/"* | "${HOME}/Desktop/"* | "${HOME}/Downloads/"*)
    echo
    echo "!! 경고: 저장소가 macOS 보호 디렉터리 아래에 있다 (${REPO_ROOT})."
    echo "   launchd 실행이 'Operation not permitted' 로 실패한다. 둘 중 하나가 필요하다:"
    echo "     (a) 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한 에 /bin/bash 추가"
    echo "         (+ 버튼 > Cmd+Shift+G > /bin/bash 입력)"
    echo "     (b) 저장소를 보호 디렉터리 밖으로 옮긴다 (예: ~/dev/regchange-copilot)"
    echo "   적용 후 확인: launchctl kickstart -k ${DOMAIN}/${LABEL}"
    echo "                launchctl print ${DOMAIN}/${LABEL} | grep 'last exit code'"
    echo
    ;;
esac

echo "등록 완료: ${LABEL}"
echo "  스케줄  매일 07:00 KST = 이 기계의 ${LOCAL_LABEL}"
echo "          (타임존·서머타임이 바뀌면 이 스크립트를 다시 돌린다)"
echo "  plist   ${TARGET}"
echo "  즉시 실행  launchctl kickstart -k ${DOMAIN}/${LABEL}"
echo "  상태 확인  launchctl print ${DOMAIN}/${LABEL} | head -20"
echo "  해제       scripts/ops/install_launchd.sh --uninstall"
