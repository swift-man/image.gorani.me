#!/bin/zsh

set -euo pipefail

label="me.gorani.image-upload"
uid="$(id -u)"
domain="gui/${uid}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
cd "${project_root}"
template_path="${project_root}/launchd/${label}.plist.template"
launcher_source="${project_root}/launchd/GoraniImageUploadLauncher.swift"
launcher_info="${project_root}/launchd/GoraniImageUpload-Info.plist"
agent_dir="${HOME}/Library/LaunchAgents"
log_dir="${HOME}/Library/Logs/gorani-image-upload"
plist_path="${agent_dir}/${label}.plist"
app_path="${HOME}/Applications/GoraniImageUpload.app"
app_contents="${app_path}/Contents"
app_executable="${app_contents}/MacOS/GoraniImageUpload"

smb_url="${GORANI_SMB_URL:-smb://ksj@DESKTOP-0217PLD/gorani-images}"
mount_point="${GORANI_SMB_MOUNT_POINT:-/Volumes/gorani-images}"
storage_root="${GORANI_LAUNCHD_STORAGE_ROOT:-${mount_point}/image-store}"
psql_bin="$(command -v psql 2>/dev/null || true)"
python_bin="${project_root}/.venv/bin/python"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "이 설치 스크립트는 macOS에서만 사용할 수 있습니다."
  exit 1
fi
if [[ ! -f "${template_path}" ]]; then
  print -u2 "LaunchAgent 템플릿을 찾을 수 없습니다: ${template_path}"
  exit 1
fi
if [[ ! -f "${launcher_source}" || ! -f "${launcher_info}" ]]; then
  print -u2 "macOS 앱 런처 소스를 찾을 수 없습니다."
  exit 1
fi
if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
  print -u2 "LaunchAgent 설치에는 Xcode Command Line Tools의 swiftc가 필요합니다."
  exit 1
fi
if [[ -z "${psql_bin}" ]]; then
  print -u2 "psql 실행 파일을 찾을 수 없습니다. PostgreSQL 설치 경로를 PATH에 추가하세요."
  exit 1
fi
if [[ ! -x "${python_bin}" ]]; then
  print -u2 "프로젝트 Python을 찾을 수 없습니다: ${python_bin}"
  exit 1
fi
if ! "${python_bin}" -m upload_service.macos_runtime smb-host \
  --smb-url "${smb_url}" >/dev/null; then
  print -u2 "SMB URL을 사용할 수 없습니다. 비밀번호는 URL 대신 macOS 키체인에 저장하세요."
  exit 1
fi
runtime_path="$(dirname "${psql_bin}"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
settings_lines=("${(@f)$(${python_bin} -c '
from upload_service.config import load_settings
settings = load_settings()
print(settings.host)
print(settings.port)
')}")
upload_host="${GORANI_LAUNCHD_UPLOAD_HOST:-${settings_lines[1]}}"
upload_port="${GORANI_LAUNCHD_UPLOAD_PORT:-${settings_lines[2]}}"

mkdir -p "${agent_dir}" "${log_dir}" "${app_contents}/MacOS"

# 앱 번들이 네트워크 볼륨 권한의 명확한 책임 주체가 되도록 네이티브 런처를 만든다.
if [[ ! -x "${app_executable}" \
  || "${launcher_source}" -nt "${app_executable}" \
  || "${launcher_info}" -nt "${app_contents}/Info.plist" ]]; then
  /usr/bin/xcrun swiftc -O "${launcher_source}" -o "${app_executable}"
  cp "${launcher_info}" "${app_contents}/Info.plist"
  /usr/bin/codesign --force --sign - --timestamp=none "${app_path}" >/dev/null
fi

# plistlib이 특수문자를 XML 규칙에 맞게 직렬화하므로 셸 문자열 치환을 사용하지 않는다.
"${python_bin}" -m upload_service.macos_runtime render-plist \
  --template "${template_path}" \
  --output "${plist_path}" \
  --app-executable "${app_executable}" \
  --supervisor-script "${project_root}/scripts/supervise-service.sh" \
  --project-root "${project_root}" \
  --runtime-path "${runtime_path}" \
  --smb-url "${smb_url}" \
  --mount-point "${mount_point}" \
  --storage-root "${storage_root}" \
  --upload-host "${upload_host}" \
  --upload-port "${upload_port}" \
  --stdout-path "${log_dir}/service.log" \
  --stderr-path "${log_dir}/service-error.log"

/usr/bin/plutil -lint "${plist_path}" >/dev/null
chmod 600 "${plist_path}"

# 이미 설치된 작업이 있으면 안전하게 내린 후 최신 plist로 다시 등록한다.
/bin/launchctl bootout "${domain}/${label}" >/dev/null 2>&1 || true
/bin/sleep 1
bootstrap_succeeded=0
for attempt in 1 2 3; do
  if /bin/launchctl bootstrap "${domain}" "${plist_path}"; then
    bootstrap_succeeded=1
    break
  fi
  print -u2 "LaunchAgent 등록을 재시도합니다. attempt=${attempt}"
  /bin/sleep 1
done
if [[ "${bootstrap_succeeded}" -ne 1 ]]; then
  print -u2 "LaunchAgent를 등록하지 못했습니다: ${plist_path}"
  exit 1
fi
/bin/launchctl enable "${domain}/${label}"
/bin/launchctl kickstart -k "${domain}/${label}"

print "LaunchAgent 설치 완료: ${plist_path}"
print "앱 런처 설치 완료: ${app_path}"
print "최초 로컬 네트워크 및 네트워크 볼륨 접근 확인 창이 표시되면 모두 '허용'을 선택하세요."
print "상태 확인: ./scripts/service-status.sh"
print "서비스 재시작: ./scripts/restart-service.sh"
