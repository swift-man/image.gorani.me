#!/bin/zsh

set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
cd "${project_root}" || exit 1
smb_url="${GORANI_SMB_URL:-smb://ksj@DESKTOP-0217PLD/gorani-images}"
mount_point="${GORANI_SMB_MOUNT_POINT:-/Volumes/gorani-images}"
storage_root="${IMAGE_STORAGE_ROOT:-${mount_point}/image-store}"
retry_seconds="${GORANI_RETRY_SECONDS:-10}"
check_seconds="${GORANI_CHECK_SECONDS:-5}"
mount_retry_max_seconds="${GORANI_MOUNT_RETRY_MAX_SECONDS:-300}"
terminate_seconds="${GORANI_TERMINATE_SECONDS:-10}"
kill_seconds="${GORANI_KILL_SECONDS:-5}"
python_bin="${project_root}/.venv/bin/python"
service_pid=""

if ! smb_host="$("${python_bin}" -m upload_service.macos_runtime smb-host \
  --smb-url "${smb_url}")"; then
  print -u2 "SMB URL을 사용할 수 없습니다. 비밀번호는 URL 대신 macOS 키체인에 저장하세요."
  exit 1
fi

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
  local mount_retry_seconds="${retry_seconds}"
  local next_mount_attempt=0

  while true; do
    if is_storage_ready; then
      return
    fi

    if is_smb_host_ready; then
      if ((SECONDS >= next_mount_attempt)); then
        log "SMB 공유 연결을 요청합니다: ${smb_url}"
        # 인증 문제가 있어도 화면을 반복 점유하지 않도록 백그라운드에서 지수 백오프로 요청한다.
        /usr/bin/open -g "${smb_url}" >/dev/null 2>&1 || true
        next_mount_attempt=$((SECONDS + mount_retry_seconds))
        mount_retry_seconds=$((mount_retry_seconds * 2))
        if ((mount_retry_seconds > mount_retry_max_seconds)); then
          mount_retry_seconds="${mount_retry_max_seconds}"
        fi
      fi
    else
      log "Windows SMB 서버를 기다립니다: ${smb_host}:445"
      mount_retry_seconds="${retry_seconds}"
      next_mount_attempt=0
    fi
    /bin/sleep "${retry_seconds}"
  done
}

trap shutdown INT TERM HUP

while true; do
  wait_for_storage
  log "SMB 저장소가 준비되었습니다: ${mount_point}"

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
