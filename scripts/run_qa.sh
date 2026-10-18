#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
for script in install.sh app/install.sh scripts/*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done
python -m compileall -q app scripts tests
./tests/test_installer_timeout_helper.sh ./app/install.sh
python -m pytest -q tests
