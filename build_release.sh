#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VERSION_FILE="$ROOT/VERSION"
version="$(tr -d '[:space:]' < "$VERSION_FILE")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Некорректная VERSION: $version" >&2; exit 1; }
ARCHIVE="$ROOT/../../FargoVPN-$version.tar.gz"
PACKAGE_NAME="$(basename "$ROOT")"
[[ "$PACKAGE_NAME" != "." && "$PACKAGE_NAME" != ".." ]] || { echo "Некорректное имя каталога проекта" >&2; exit 1; }
rm -f "$ARCHIVE"
tar -czf "$ARCHIVE" \
  --exclude="$PACKAGE_NAME/config.py" \
  --exclude="$PACKAGE_NAME/config.py.before_*" \
  --exclude="$PACKAGE_NAME/data" \
  --exclude="$PACKAGE_NAME/.venv" \
  --exclude="$PACKAGE_NAME/__pycache__" \
  --exclude="$PACKAGE_NAME/.pytest_cache" \
  --exclude="$PACKAGE_NAME/*/.pytest_cache" \
  --exclude="$PACKAGE_NAME/*/__pycache__" \
  --exclude="$PACKAGE_NAME/*.pyc" \
  -C "$ROOT/.." "$PACKAGE_NAME"
printf 'Собран FargoVPN %s\n' "$version"
printf 'Архив: %s\n' "$ARCHIVE"
printf 'SHA-256: %s\n' "$(sha256sum "$ARCHIVE" | awk '{print $1}')"
