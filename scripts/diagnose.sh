#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../app" && pwd)"
PYTHON="${PYTHON:-python3}"
OUT_DIR="${FARGOVPN_DIAG_OUT_DIR:-/var/log/fargovpn}"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT
set +e
"$PYTHON" "$ROOT_DIR/diagnose.py" --app-dir "$ROOT_DIR" --output-dir "$OUT_DIR" --json "$@" >"$TMP_OUT" 2>&1
RC=$?
cat "$TMP_OUT"
mapfile -t DIAG_META < <("$PYTHON" - "$TMP_OUT" <<'PY2'
import json,sys
text=open(sys.argv[1],encoding="utf-8",errors="replace").read().strip()
pos=text.find("{")
if pos < 0:
    raise SystemExit
try:
    data=json.JSONDecoder().raw_decode(text[pos:])[0]
except Exception:
    raise SystemExit
checks=data.get("checks") or []
print(str(data.get("path") or ""))
print("PASS=%d FAIL=%d SKIP=%d" % (
    sum(c.get("status")=="PASS" for c in checks),
    sum(c.get("status")=="FAIL" for c in checks),
    sum(c.get("status")=="SKIP" for c in checks),
))
PY2
)
LOG_PATH="${DIAG_META[0]:-}"
SUMMARY="${DIAG_META[1]:-PASS=0 FAIL=0 SKIP=0}"
if [[ -n "$LOG_PATH" && -f "$LOG_PATH" ]]; then
  echo "DIAGNOSE_LOG=$LOG_PATH"
else
  echo "DIAGNOSE_LOG=<not-created>"
fi
echo "SUMMARY $SUMMARY"
exit "$RC"
