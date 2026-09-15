#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VERSION_FILE="$ROOT/VERSION"

read_version() {
  local value
  value="$(tr -d '[:space:]' < "$VERSION_FILE")"
  [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Некорректная версия в VERSION: '$value'" >&2
    exit 1
  }
  printf '%s\n' "$value"
}

increment_patch() {
  local current major minor patch
  current="$(read_version)"
  IFS=. read -r major minor patch <<< "$current"
  printf '%s.%s.%s\n' "$major" "$minor" "$((patch + 1))"
}

CURRENT="$(read_version)"
NEXT="$(increment_patch)"

case "${1:-patch}" in
  patch)
    printf '%s\n' "$NEXT" > "$VERSION_FILE"
    python3 - "$ROOT" "$NEXT" <<'PYUPD'
from pathlib import Path
import re, sys
root=Path(sys.argv[1]); nxt=sys.argv[2]
readme=root/"README.md"
s=readme.read_text(encoding="utf-8")
s=re.sub(r"(Текущая версия:\s*\*\*)[0-9]+\.[0-9]+\.[0-9]+(\*\*)", rf"\g<1>{nxt}\g<2>", s, count=1)
readme.write_text(s,encoding="utf-8")
notes=root/f"RELEASE_NOTES_{nxt}.md"
if not notes.exists():
    notes.write_text(f"# FargoVPN {nxt}\n\n## Изменения\n\n- Заполнить changelog перед публикацией релиза.\n",encoding="utf-8")
PYUPD
    echo "Версия: $CURRENT -> $NEXT"
    ;;
  show)
    echo "$CURRENT"
    ;;
  *)
    echo "Использование: $0 [patch|show]" >&2
    exit 2
    ;;
esac
