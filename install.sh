#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW="https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main"
ARCHIVE_NAME="FargoVPN_FULL.tar.gz"
ARCHIVE_URL="${FARGOVPN_ARCHIVE_URL:-$REPO_RAW/$ARCHIVE_NAME}"
CHECKSUM_URL="${FARGOVPN_CHECKSUM_URL:-$REPO_RAW/$ARCHIVE_NAME.sha256}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Запустите установщик через sudo или от пользователя root." >&2
  exit 1
fi

if ! command -v wget >/dev/null 2>&1; then
  echo "Команда wget не найдена. Устанавливаю wget и ca-certificates..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y wget ca-certificates
fi

command -v tar >/dev/null 2>&1 || { echo "Не найдена обязательная команда tar." >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "Не найдена обязательная команда sha256sum." >&2; exit 1; }

TMP="$(mktemp -d /tmp/fargovpn-bootstrap.XXXXXX)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

ARCHIVE="$TMP/$ARCHIVE_NAME"
SUMFILE="$TMP/$ARCHIVE_NAME.sha256"

printf '
[FargoVPN bootstrap] Скачивание актуального полного пакета...
'
wget --https-only --tries=3 --timeout=30 --show-progress -O "$ARCHIVE" "$ARCHIVE_URL"

printf '
[FargoVPN bootstrap] Скачивание SHA-256...
'
wget --https-only --tries=3 --timeout=30 -qO "$SUMFILE" "$CHECKSUM_URL"

printf '[FargoVPN bootstrap] Проверка SHA-256...
'
( cd "$TMP"; sha256sum -c "$(basename "$SUMFILE")" )

printf '[FargoVPN bootstrap] Проверка архива...
'
tar -tzf "$ARCHIVE" >/dev/null

TOP_DIRS=( $(tar -tzf "$ARCHIVE" | awk -F/ 'NF {print $1}' | sort -u) )
if [[ ${#TOP_DIRS[@]} -ne 1 ]]; then
  echo "Не удалось определить единственный корневой каталог полного пакета." >&2
  exit 1
fi

PACKAGE_ROOT="$TMP/${TOP_DIRS[0]}"
tar -xzf "$ARCHIVE" -C "$TMP"

[[ -f "$PACKAGE_ROOT/install.sh" ]] || { echo "В полном пакете не найден install.sh." >&2; exit 1; }
[[ -f "$PACKAGE_ROOT/VERSION" ]] || { echo "В полном пакете не найден VERSION." >&2; exit 1; }

VERSION="$(tr -d '[:space:]' < "$PACKAGE_ROOT/VERSION")"
printf '[FargoVPN bootstrap] Версия полного пакета: %s
' "$VERSION"
printf '[FargoVPN bootstrap] Запуск штатного установщика...

'

ARGS=("$@")
if [[ ${#ARGS[@]} -eq 0 ]]; then
  ARGS=(--profile full)
fi

exec /bin/bash "$PACKAGE_ROOT/install.sh" "${ARGS[@]}"
