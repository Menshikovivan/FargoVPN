#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/panel_runtime.py"
