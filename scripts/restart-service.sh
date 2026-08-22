#!/bin/zsh

set -euo pipefail

label="me.gorani.image-upload"
domain="gui/$(id -u)"
script_dir="$(cd "$(dirname "$0")" && pwd)"

if /bin/launchctl print "${domain}/${label}" >/dev/null 2>&1; then
  /bin/launchctl kickstart -k "${domain}/${label}"
  print "LaunchAgent 서비스를 재시작했습니다: ${label}"
  exit 0
fi

print "LaunchAgent가 설치되지 않아 포그라운드로 실행합니다."
exec "${script_dir}/run-service.sh"
