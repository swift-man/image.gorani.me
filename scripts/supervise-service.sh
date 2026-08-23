#!/bin/zsh

set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
smb_host="${GORANI_SMB_HOST:-DESKTOP-0217PLD}"
smb_url="${GORANI_SMB_URL:-smb://ksj@DESKTOP-0217PLD/gorani-images}"
mount_point="${GORANI_SMB_MOUNT_POINT:-/Volumes/gorani-images}"
storage_root="${IMAGE_STORAGE_ROOT:-${mount_point}/image-store}"
retry_seconds="${GORANI_RETRY_SECONDS:-10}"
check_seconds="${GORANI_CHECK_SECONDS:-5}"
terminate_seconds="${GORANI_TERMINATE_SECONDS:-10}"
kill_seconds="${GORANI_KILL_SECONDS:-5}"
python_bin="${project_root}/.venv/bin/python"
service_pid=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

is_storage_ready() {
  "${python_bin}" -m upload_service.macos_runtime check-storage \
    --smb-url "${smb_url}" \
    --mount-point "${mount_point}" \
    --storage-root "${storage_root}" \
    --timeout 2 >/dev/null 2>&1
}

is_smb_host_ready() {
  /usr/bin/nc -G 2 -z "${smb_host}" 445 >/dev/null 2>&1
}

stop_service() {
  if [[ -z "${service_pid}" ]]; then
    return
  fi
  if /bin/kill -0 "${service_pid}" 2>/dev/null; then
    log "업로드 서비스를 중지합니다. pid=${service_pid}"
    /bin/kill -TERM "${service_pid}" 2>/dev/null || true
  fi
  for ((elapsed = 0; elapsed < terminate_seconds; elapsed++)); do
    if ! /bin/kill -0 "${service_pid}" 2>/dev/null; then
      wait "${service_pid}" 2>/dev/null || true
      service_pid=""
      return 0
    fi
    /bin/sleep 1
  done

  log "정상 종료 제한시간을 초과해 서비스를 강제 종료합니다. pid=${service_pid}"
  /bin/kill -KILL "${service_pid}" 2>/dev/null || true
  for ((elapsed = 0; elapsed < kill_seconds; elapsed++)); do
    if ! /bin/kill -0 "${service_pid}" 2>/dev/null; then
      wait "${service_pid}" 2>/dev/null || true
      service_pid=""
      return 0
    fi
    /bin/sleep 1
  done

  log "서비스 프로세스를 회수하지 못했습니다. pid=${service_pid}"
  service_pid=""
  return 1
}

shutdown() {
  stop_service || true
  exit 0
}

wait_for_storage() {
  while true; do
    if is_storage_ready; then
      return
    fi

    if is_smb_host_ready; then
      log "SMB 공유 연결을 요청합니다: ${smb_url}"
      # Finder가 키체인에 저장된 자격 증명으로 /Volumes 아래에 공유를 연결한다.
      /usr/bin/open "${smb_url}" >/dev/null 2>&1 || true
    else
      log "Windows SMB 서버를 기다립니다: ${smb_host}:445"
    fi
    /bin/sleep "${retry_seconds}"
  done
}

trap shutdown INT TERM HUP

while true; do
  wait_for_storage
  log "SMB 저장소가 준비되었습니다: ${mount_point}"

  cd "${project_root}" || exit 1
  "${project_root}/scripts/run-service.sh" &
  service_pid=$!
  log "업로드 서비스를 시작했습니다. pid=${service_pid}"

  while /bin/kill -0 "${service_pid}" 2>/dev/null; do
    /bin/sleep "${check_seconds}"
    if ! is_storage_ready; then
      log "SMB 연결이 끊겨 업로드 서비스를 안전하게 중지합니다."
      if ! stop_service; then
        exit 1
      fi
      break
    fi
  done

  if [[ -n "${service_pid}" ]]; then
    wait "${service_pid}" 2>/dev/null
    exit_code=$?
    log "업로드 서비스가 종료되었습니다. exit=${exit_code}"
    service_pid=""
  fi
  /bin/sleep "${retry_seconds}"
done
