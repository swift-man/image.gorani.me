#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"

export IMAGE_UPLOAD_HOST="${IMAGE_UPLOAD_HOST:-127.0.0.1}"
export IMAGE_UPLOAD_PORT="${IMAGE_UPLOAD_PORT:-8080}"
export IMAGE_STORAGE_ROOT="${IMAGE_STORAGE_ROOT:-/Volumes/gorani-images/image-store}"
export IMAGE_PUBLIC_PREFIX="${IMAGE_PUBLIC_PREFIX:-/i}"
export IMAGE_MAX_UPLOAD_BYTES="${IMAGE_MAX_UPLOAD_BYTES:-20971520}"
export IMAGE_ENABLE_THUMBNAILS="${IMAGE_ENABLE_THUMBNAILS:-1}"
export IMAGE_THUMBNAIL_WIDTHS="${IMAGE_THUMBNAIL_WIDTHS:-160,320,640}"
export IMAGE_THUMBNAIL_FORMAT="${IMAGE_THUMBNAIL_FORMAT:-webp}"
export PGDATABASE="${PGDATABASE:-postgres}"

cd "${project_root}"

if [ -x "${python_bin}" ]; then
  exec "${python_bin}" -m upload_service
fi

exec python3 -m upload_service
