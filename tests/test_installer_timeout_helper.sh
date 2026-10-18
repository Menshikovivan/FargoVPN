#!/usr/bin/env bash
set -euo pipefail
INSTALLER="$1"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# Extract helper block between run_with_timeout and require_command without executing installer side effects.
awk '/^run_with_timeout\(\) \{/{p=1} /^require_command\(\) \{/{p=0} p' "$INSTALLER" > "$TMP/helper.sh"
source "$TMP/helper.sh"
start=$(date +%s)
set +e
run_with_timeout 1 sleep 3
rc=$?
set -e
elapsed=$(( $(date +%s) - start ))
[[ "$rc" == "124" ]]
(( elapsed <= 3 ))
run_with_timeout 3 true
printf 'installer timeout helper: PASS\n'
