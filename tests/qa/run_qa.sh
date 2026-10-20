#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT"
export PYTHONPATH="$ROOT/app:${PYTHONPATH:-}"

printf '%s\n' '== FargoVPN QA suite =='
python3 -m py_compile app/*.py app/services/*.py tests/test_release_512_regressions.py tests/qa/*.py
for f in app/static/*.js; do node --check "$f" >/dev/null; done
bash -n app/install.sh
bash -n app/web_start.sh

QA_RENDER_DIR="${FARGOVPN_QA_RENDER_DIR:-$ROOT/.qa/rendered}"
QA_RENDER_DIR="$(realpath -m -- "$QA_RENDER_DIR")"
QA_SAFE_ROOT="$ROOT/.qa"
case "$QA_RENDER_DIR" in
  "$QA_SAFE_ROOT"/*) ;;
  *) echo 'Refusing to remove a render directory outside this project .qa/.' >&2; exit 2 ;;
esac
rm -rf -- "$QA_RENDER_DIR"
PYTHONPATH="$ROOT/app:${PYTHONPATH:-}" python3 tests/qa/render_panel.py "$ROOT" "$QA_RENDER_DIR"
FARGOVPN_RENDERED_DIR="$QA_RENDER_DIR" python3 tests/qa/browser_panel_audit.py

pytest -q

if [[ -n "${FARGOVPN_DATABASE_URL:-}" && "${FARGOVPN_QA_ALLOW_SEED_DISPOSABLE:-}" == "1" ]]; then
  case "${FARGOVPN_DATABASE_URL,,}" in
    postgresql*)
      python3 tests/qa/seed_test_db.py --allow-disposable-loopback --users "${FARGOVPN_QA_USERS:-300}" --messages "${FARGOVPN_QA_MESSAGES:-1500}" --logs "${FARGOVPN_QA_LOGS:-1200}"
      ;;
    *)
      echo 'QA DB seed skipped: FARGOVPN_DATABASE_URL is not PostgreSQL.'
      ;;
  esac
else
  echo 'QA DB seed skipped: requires an explicit loopback QA DSN and FARGOVPN_QA_ALLOW_SEED_DISPOSABLE=1.'
fi

echo 'QA checks completed.'
