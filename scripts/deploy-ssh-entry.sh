#!/bin/sh
# authorized_keys의 강제 명령으로 설치한다. 입력을 eval하거나 인자로 전달하지 않는다.
set -eu
if [ "${SSH_ORIGINAL_COMMAND:-}" != 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"' ]; then
  echo 'Denied: only image deployment reconciliation is allowed' >&2
  exit 64
fi
# 허용 문자열은 요청 식별자다. 실행 경로는 키에 등록된 진입점의 설치 위치로 한정한다.
entry_dir="$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)"
exec "$entry_dir/sync.sh"
