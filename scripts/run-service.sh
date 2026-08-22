#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
python_bin="${project_root}/.venv/bin/python"

cd "${project_root}"

if [ -x "${python_bin}" ]; then
  exec "${python_bin}" -m upload_service
fi

exec python3 -m upload_service
