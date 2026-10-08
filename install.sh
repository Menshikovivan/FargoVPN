#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW="https://raw.githubusercontent.com/Menshikovivan/FargoVPN/main"
ARCHIVE_NAME="FargoVPN_FULL.tar.gz"
ARCHIVE_URL="${FARGOVPN_ARCHIVE_URL:-$REPO_RAW/$ARCHIVE_NAME}"
CHECKSUM_URL="${FARGOVPN_CHECKSUM_URL:-$REPO_RAW/$ARCHIVE_NAME.sha256}"
TMP_BASE="${FARGOVPN_BOOTSTRAP_TMPDIR:-/var/tmp}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Запустите установщик через sudo или от пользователя root." >&2
  exit 1
fi

mkdir -p "$TMP_BASE"

download() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 -o "$destination" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --https-only --tries=3 --timeout=30 -O "$destination" "$url"
  else
    echo "Не найдены curl и wget. Устанавливаю curl и ca-certificates..." >&2
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends curl ca-certificates
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 -o "$destination" "$url"
  fi
}

missing_packages=()
if ! command -v tar >/dev/null 2>&1; then missing_packages+=(tar); fi
if ! command -v sha256sum >/dev/null 2>&1; then missing_packages+=(coreutils); fi
if (( ${#missing_packages[@]} )); then
  echo "[FargoVPN bootstrap] Устанавливаю недостающие системные зависимости: ${missing_packages[*]}..." >&2
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${missing_packages[@]}"
fi
command -v tar >/dev/null 2>&1 || { echo "Не удалось установить tar." >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "Не удалось установить sha256sum." >&2; exit 1; }

choose_tmp_base() {
  local candidate free
  local candidates=("$TMP_BASE" "${TMPDIR:-}" "/var/tmp" "/tmp" "/root/.cache")
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    mkdir -p "$candidate" 2>/dev/null || continue
    free=$(df -Pk -- "$candidate" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ "$free" =~ ^[0-9]+$ ]] && (( free >= 262144 )); then
      TMP_BASE="$candidate"
      echo "[FargoVPN bootstrap] Временные файлы: $TMP_BASE (свободно около $((free / 1024)) МБ)"
      return 0
    fi
  done
  echo "Не найден доступный временный каталог минимум с 256 МБ свободного места." >&2
  echo "Освободите место или задайте FARGOVPN_BOOTSTRAP_TMPDIR на подходящем разделе." >&2
  return 1
}
choose_tmp_base

TMP="$(mktemp -d "$TMP_BASE/fargovpn-bootstrap.XXXXXX")" || {
  echo "Не удалось создать временный каталог для FargoVPN в $TMP_BASE." >&2
  exit 1
}
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

ARCHIVE="$TMP/$ARCHIVE_NAME"
SUMFILE="$TMP/$ARCHIVE_NAME.sha256"

echo "[FargoVPN bootstrap] Получение актуального полного пакета..."
download "$ARCHIVE_URL" "$ARCHIVE" || { echo "Не удалось скачать полный пакет FargoVPN." >&2; exit 1; }
download "$CHECKSUM_URL" "$SUMFILE" || { echo "Не удалось скачать SHA-256 полного пакета FargoVPN." >&2; exit 1; }

echo "[FargoVPN bootstrap] Проверка SHA-256..."
( cd "$TMP" && sha256sum -c "$(basename "$SUMFILE")" ) || { echo "Проверка SHA-256 не пройдена; установка остановлена." >&2; exit 1; }

echo "[FargoVPN bootstrap] Проверка структуры архива..."
tar -tzf "$ARCHIVE" >/dev/null || { echo "Архив FargoVPN повреждён или имеет неверный формат." >&2; exit 1; }
TOP_DIRS=( $(tar -tzf "$ARCHIVE" | awk -F/ 'NF {print $1}' | sort -u) )
if [[ ${#TOP_DIRS[@]} -ne 1 ]]; then
  echo "Не удалось определить единственный корневой каталог полного пакета." >&2
  exit 1
fi
PACKAGE_ROOT="$TMP/${TOP_DIRS[0]}"
tar -xzf "$ARCHIVE" -C "$TMP" || { echo "Не удалось распаковать полный пакет FargoVPN во временный каталог." >&2; exit 1; }

[[ -f "$PACKAGE_ROOT/install.sh" ]] || { echo "В полном пакете не найден install.sh." >&2; exit 1; }
[[ -f "$PACKAGE_ROOT/VERSION" ]] || { echo "В полном пакете не найден VERSION." >&2; exit 1; }
VERSION="$(tr -d '[:space:]' < "$PACKAGE_ROOT/VERSION")"
echo "[FargoVPN bootstrap] Версия полного пакета: $VERSION"
echo "[FargoVPN bootstrap] Запуск штатного установщика..."

set +e
/bin/bash "$PACKAGE_ROOT/install.sh" "$@"
STATUS=$?
set -e
exit "$STATUS"
