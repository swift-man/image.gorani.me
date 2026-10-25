#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
project_root="$(cd "${script_dir}/.." && pwd -P)"
python_bin="${project_root}/.venv/bin/python"

cd "${project_root}"
# current 심볼릭 링크가 전환되어도 실행 중인 프로세스는 자기 release를 유지한다.
export IMAGE_RELEASE_SHA="unmanaged"
if [[ -f "${project_root}/.release-sha" ]]; then
  export IMAGE_RELEASE_SHA="$(<"${project_root}/.release-sha")"
fi

if [ -x "${python_bin}" ]; then
  exec "${python_bin}" -m upload_service
fi

exec python3 -m upload_service
