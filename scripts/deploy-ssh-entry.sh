#!/bin/sh
# authorized_keys의 강제 명령으로 설치한다. 입력을 eval하거나 인자로 전달하지 않는다.
set -eu
if [ "${SSH_ORIGINAL_COMMAND:-}" != 'exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"' ]; then
  echo 'Denied: only image deployment reconciliation is allowed' >&2
  exit 64
fi
exec "$HOME/Library/Application Support/gorani-image-upload/deploy/sync.sh"
