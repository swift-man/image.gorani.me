#!/bin/zsh

set -u

label="me.gorani.image-upload"
domain="gui/$(id -u)"
script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"
plist_path="${HOME}/Library/LaunchAgents/${label}.plist"
overall_status=0

agent_value() {
  local key="$1"
  local fallback="$2"
  if [[ -f "${plist_path}" ]]; then
    /usr/bin/plutil -extract "EnvironmentVariables.${key}" raw -o - \
      "${plist_path}" 2>/dev/null && return
  fi
  print -r -- "${fallback}"
}

mount_point="$(agent_value GORANI_SMB_MOUNT_POINT /Volumes/gorani-images)"
smb_url="$(agent_value GORANI_SMB_URL smb://ksj@DESKTOP-0217PLD/gorani-images)"
storage_root="$(agent_value IMAGE_STORAGE_ROOT "${mount_point}/image-store")"
upload_host="$(agent_value IMAGE_UPLOAD_HOST 127.0.0.1)"
upload_port="$(agent_value IMAGE_UPLOAD_PORT 8080)"

case "${upload_host}" in
  ""|0.0.0.0|::) health_host="127.0.0.1" ;;
  *:*) health_host="[${upload_host}]" ;;
  *) health_host="${upload_host}" ;;
esac
health_url="http://${health_host}:${upload_port}/healthz"

if [[ "${1:-}" == "--health-url" ]]; then
  print -r -- "${health_url}"
  exit 0
fi

if "${python_bin}" -m upload_service.macos_runtime check-storage \
  --smb-url "${smb_url}" \
  --mount-point "${mount_point}" \
  --storage-root "${storage_root}" \
  --timeout 2 >/dev/null 2>&1; then
  print "SMB: mounted (${mount_point})"
else
  print "SMB: disconnected (${mount_point})"
  overall_status=1
fi

if /bin/launchctl print "${domain}/${label}" >/dev/null 2>&1; then
  print "LaunchAgent: loaded (${label})"
else
  print "LaunchAgent: not loaded (${label})"
  overall_status=1
fi

if /usr/bin/curl --fail --silent --max-time 2 "${health_url}" >/dev/null; then
  print "HTTP: healthy (${health_url})"
else
  print "HTTP: unavailable (${health_url})"
  overall_status=1
fi

exit "${overall_status}"
