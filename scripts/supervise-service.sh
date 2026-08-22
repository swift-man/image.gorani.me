#!/bin/zsh

set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
smb_host="${GORANI_SMB_HOST:-DESKTOP-0217PLD}"
smb_url="${GORANI_SMB_URL:-smb://ksj@DESKTOP-0217PLD/gorani-images}"
mount_point="${GORANI_SMB_MOUNT_POINT:-/Volumes/gorani-images}"
retry_seconds="${GORANI_RETRY_SECONDS:-10}"
check_seconds="${GORANI_CHECK_SECONDS:-5}"
service_pid=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

is_smb_mounted() {
  /sbin/mount | /usr/bin/grep -Fq -- " on ${mount_point} (smbfs,"
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
  wait "${service_pid}" 2>/dev/null || true
  service_pid=""
}

shutdown() {
  stop_service
  exit 0
}

wait_for_storage() {
  while true; do
    if is_smb_mounted; then
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
    if ! is_smb_mounted; then
      log "SMB 연결이 끊겨 업로드 서비스를 안전하게 중지합니다."
      stop_service
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
