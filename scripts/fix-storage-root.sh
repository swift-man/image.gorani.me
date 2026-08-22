#!/bin/zsh

set -euo pipefail

script_dir="${0:A:h}"
repo_root="${script_dir:h}"
python_bin="${PYTHON_BIN:-${repo_root}/.venv/bin/python}"

if [[ ! -x "${python_bin}" ]]; then
  print -u2 "Python runtime not found: ${python_bin}"
  exit 1
fi

# 프로젝트 모듈과 .env를 동일하게 찾도록 저장소 루트에서 관리 명령을 실행한다.
cd "${repo_root}"
exec "${python_bin}" -m upload_service.fix_storage_root
