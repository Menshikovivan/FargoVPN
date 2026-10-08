#!/usr/bin/env bash
set -euo pipefail
BASE=$(cd -- "$(dirname -- "$0")" && pwd)
APP_DIR="$BASE"
ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do
  if [[ "${ARGS[$i]}" == "--app-dir" && $((i+1)) -lt ${#ARGS[@]} ]]; then APP_DIR="${ARGS[$((i+1))]}"; fi
done
PYTHON="${FARGOVPN_PYTHON:-python3}"
if [[ -z "${FARGOVPN_PYTHON:-}" && -x "$APP_DIR/.venv/bin/python" ]]; then PYTHON="$APP_DIR/.venv/bin/python"; fi
exec "$PYTHON" "$BASE/diagnose.py" "$@"
