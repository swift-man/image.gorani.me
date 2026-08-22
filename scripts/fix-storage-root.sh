#!/bin/zsh

set -euo pipefail

old_root="${OLD_STORAGE_ROOT:-${HOME}/mnt/gorani-images/image-store}"
new_root="${NEW_STORAGE_ROOT:-/Volumes/gorani-images/image-store}"

psql -X -v ON_ERROR_STOP=1 \
  -v old_root="${old_root}" \
  -v new_root="${new_root}" <<'SQL'
BEGIN;

UPDATE assets
SET storage_path = :'new_root' || substr(storage_path, length(:'old_root') + 1)
WHERE left(storage_path, length(:'old_root') + 1) = :'old_root' || '/';

UPDATE asset_variants
SET storage_path = :'new_root' || substr(storage_path, length(:'old_root') + 1)
WHERE left(storage_path, length(:'old_root') + 1) = :'old_root' || '/';

COMMIT;
SQL

echo "Updated storage root:"
echo "  from: ${old_root}"
echo "    to: ${new_root}"
