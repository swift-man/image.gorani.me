#!/bin/zsh

set -euo pipefail

label="me.gorani.image-upload"
domain="gui/$(id -u)"
plist_path="${HOME}/Library/LaunchAgents/${label}.plist"
app_path="${HOME}/Applications/GoraniImageUpload.app"

/bin/launchctl bootout "${domain}/${label}" >/dev/null 2>&1 || true
rm -f "${plist_path}"
rm -rf "${app_path}"

print "LaunchAgent를 제거했습니다: ${label}"
