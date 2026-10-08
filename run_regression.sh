#!/usr/bin/env bash
set -euo pipefail
BASE=$(cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$BASE/.qa-venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then python3 -m venv "$BASE/.qa-venv"; fi
"$PYTHON" -m pip install -r "$BASE/requirements-dev.txt" playwright
"$PYTHON" -m playwright install --with-deps chromium
exec "$PYTHON" "$BASE/scripts/run_regression.py" "$@"
