#!/bin/zsh

set -euo pipefail

label="me.gorani.image-upload"
domain="gui/$(id -u)"
script_dir="$(cd "$(dirname "$0")" && pwd)"
health_url="${GORANI_HEALTH_URL:-$("${script_dir}/service-status.sh" --health-url)}"
timeout_seconds="${GORANI_RESTART_TIMEOUT_SECONDS:-30}"
service_log="${HOME}/Library/Logs/gorani-image-upload/service.log"
error_log="${HOME}/Library/Logs/gorani-image-upload/service-error.log"

show_recent_logs() {
  if [[ -f "${service_log}" ]]; then
    print "최근 서비스 로그:"
    /usr/bin/tail -n 5 "${service_log}"
  fi
}

show_error_logs() {
  if [[ -f "${error_log}" ]]; then
    print -u2 "최근 오류 로그:"
    /usr/bin/tail -n 15 "${error_log}" >&2
  fi
}

if /bin/launchctl print "${domain}/${label}" >/dev/null 2>&1; then
  /bin/launchctl kickstart -k "${domain}/${label}"
  print "LaunchAgent에 재시작을 요청했습니다: ${label}"

  deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    if /usr/bin/curl --fail --silent --max-time 2 "${health_url}" >/dev/null; then
      print "서비스 재시작 완료: ${health_url}"
      "${script_dir}/service-status.sh"
      show_recent_logs
      exit 0
    fi
    if ((SECONDS < deadline)); then
      /bin/sleep 1
    fi
  done

  print -u2 "서비스가 ${timeout_seconds}초 안에 정상 상태가 되지 않았습니다."
  "${script_dir}/service-status.sh" >&2 || true
  show_recent_logs >&2
  show_error_logs
  exit 1
fi

print "LaunchAgent가 설치되지 않아 포그라운드로 실행합니다."
exec "${script_dir}/run-service.sh"
