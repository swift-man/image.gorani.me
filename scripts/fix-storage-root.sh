#!/bin/zsh

set -euo pipefail

old_root="${OLD_STORAGE_ROOT:-/Users/m4_26/mnt/gorani-images/image-store}"
new_root="${NEW_STORAGE_ROOT:-/Volumes/gorani-images/image-store}"

psql -X -v ON_ERROR_STOP=1 <<SQL
UPDATE assets
SET storage_path = replace(storage_path, '${old_root}', '${new_root}')
WHERE storage_path LIKE '${old_root}/%';

UPDATE asset_variants
SET storage_path = replace(storage_path, '${old_root}', '${new_root}')
WHERE storage_path LIKE '${old_root}/%';
SQL

echo "Updated storage root:"
echo "  from: ${old_root}"
echo "    to: ${new_root}"
