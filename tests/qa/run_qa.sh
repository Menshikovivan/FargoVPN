#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/app:${PYTHONPATH:-}"

printf '%s\n' '== FargoVPN 5.1.19 QA suite =='
python3 -m py_compile app/*.py app/services/*.py tests/test_release_512_regressions.py tests/qa/*.py
for f in app/static/*.js; do node --check "$f" >/dev/null; done
bash -n app/install.sh
bash -n app/web_start.sh

QA_RENDER_DIR="${FARGOVPN_QA_RENDER_DIR:-$ROOT/.qa/rendered}"
rm -rf "$QA_RENDER_DIR"
PYTHONPATH="$ROOT/app:${PYTHONPATH:-}" python3 tests/qa/render_panel.py "$ROOT" "$QA_RENDER_DIR"
FARGOVPN_RENDERED_DIR="$QA_RENDER_DIR" python3 tests/qa/browser_panel_audit.py

pytest -q

if [[ -n "${FARGOVPN_DATABASE_URL:-}" ]]; then
  case "${FARGOVPN_DATABASE_URL,,}" in
    postgresql*)
      python3 tests/qa/seed_test_db.py --users "${FARGOVPN_QA_USERS:-300}" --messages "${FARGOVPN_QA_MESSAGES:-1500}" --logs "${FARGOVPN_QA_LOGS:-1200}"
      ;;
    *)
      echo 'QA DB seed skipped: FARGOVPN_DATABASE_URL is not PostgreSQL.'
      ;;
  esac
else
  echo 'QA DB seed skipped: no FARGOVPN_DATABASE_URL was provided (this is intentional; production data is never touched).'
fi

echo 'QA checks completed.'
