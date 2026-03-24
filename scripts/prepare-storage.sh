#!/bin/zsh

set -euo pipefail

storage_root="${IMAGE_STORAGE_ROOT:-/Volumes/gorani-images/image-store}"

mkdir -p "${storage_root}/original"
mkdir -p "${storage_root}/variants"

echo "Prepared storage:"
echo "  ${storage_root}/original"
echo "  ${storage_root}/variants"
