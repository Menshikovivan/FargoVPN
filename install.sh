#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONUTF8=1

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Запустите установщик через sudo или от пользователя root.'; exit 1; }

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$PACKAGE_ROOT"
DEFAULT_TARGET="/root/vpn_bot"
TARGET="$DEFAULT_TARGET"
# TARGET is consumed by child Python processes during installation. Export it early.
export TARGET
BACKUP="/var/backups/vpn-service"
VERSION="$(cat "$SRC/VERSION" 2>/dev/null)"
[[ -n "$VERSION" ]] || VERSION="4.0"
TMP=""
MASK_TMP_FILE=""
PREUPDATE_BACKUP=""
PREUPDATE_BACKUP_PART=""
PREUPDATE_SYSTEMD_DIR=""
NONINTERACTIVE=0
MODE=""
OLD=""
RESTORE=""
PROFILE=""
PROFILE_EXPLICIT=0
UPDATE_PATH=""
MASK_MODE="auto"
MASK_MODE_EXPLICIT=0
MASK_DETECTION_REASON=""
MASK_ACTION="skip"
MASK_LATEST_REF=""
MASK_READY=0
MASK_ACCESS_PRINTED=0

usage() {
  cat <<USAGE
Использование: $0 [--profile full|lite] [--mask auto|yes|no] [--update-existing /путь/к/приложению]

Без параметра --update-existing установщик предлагает:
  1) новую установку
  2) обновление существующей установки
  3) восстановление конфигурации и баз данных из резервной копии

USAGE
}

while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo 'После --profile необходимо указать значение full или lite.' >&2; exit 2; }
      PROFILE="${2,,}"
      PROFILE_EXPLICIT=1
      shift 2
      ;;
    --mask)
      [[ $# -ge 2 ]] || { echo 'После --mask необходимо указать auto, yes или no.' >&2; exit 2; }
      MASK_MODE="${2,,}"
      MASK_MODE_EXPLICIT=1
      shift 2
      ;;
    --update-existing)
      [[ $# -ge 2 ]] || { echo 'После --update-existing необходимо указать путь к существующей установке.' >&2; exit 2; }
      UPDATE_PATH=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ $MASK_MODE == auto || $MASK_MODE == yes || $MASK_MODE == no ]] || { echo 'Режим mask должен быть auto, yes или no.' >&2; exit 2; }

[[ -z $PROFILE || $PROFILE == full || $PROFILE == lite ]] || {
  echo 'Профиль должен иметь значение full или lite.' >&2
  exit 2
}

INSTALL_LOG="/var/log/vpn-service-install.log"
mkdir -p "$(dirname "$INSTALL_LOG")"
# Сохраняем полный журнал установки, не скрывая вывод в терминале.
exec > >(tee -a "$INSTALL_LOG") 2>&1

log_step() {
  echo
  echo "[Установщик VPN Service Platform] $*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Не найдена обязательная команда: $1" >&2
    exit 1
  }
}

preflight() {
  log_step "Предварительная проверка системы"
  require_command bash
  require_command python3
  require_command systemctl
  require_command tar

  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Требуется Python 3.10 или новее; обнаружена версия {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]}: версия подходит")
PY

  if [[ ! -d /run/systemd/system ]]; then
    echo 'systemd не запущен. Для установки нужен обычный сервер Ubuntu/Debian, загруженный с systemd.' >&2
    exit 1
  fi

  local free_kb
  free_kb=$(df -Pk "$PACKAGE_ROOT" | awk 'NR==2 {print $4}')
  if [[ ${free_kb:-0} -lt 1048576 ]]; then
    echo 'Для установки требуется не менее 1 ГБ свободного места на диске.' >&2
    exit 1
  fi
}

apt_install() {
  local packages=("$@")
  local missing=()
  local package status attempt
  for package in "${packages[@]}"; do
    if status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null); then :; else status=""; fi
    [[ $status == 'install ok installed' ]] || missing+=("$package")
  done
  if ((${#missing[@]} == 0)); then
    log_step "Системные зависимости уже установлены; запуск APT пропущен"
    return 0
  fi

  log_step "Устанавливаются недостающие системные зависимости: ${missing[*]}"
  for attempt in 1 2 3; do
    if apt-get update && apt-get install -y --no-install-recommends "${missing[@]}"; then
      return 0
    fi
    echo "Попытка APT №$attempt завершилась ошибкой; выполняется повтор..." >&2
    sleep 3
  done
  echo 'Не удалось установить пакеты через APT. Подробности: /var/log/vpn-service-install.log.' >&2
  return 1
}

pip_install() {
  local requirements=$1
  local stamp="$TARGET/.venv/.vpn-requirements.sha256"
  local digest installed=''
  digest=$(python3 - "$requirements" <<'PYREQ'
from hashlib import sha256
from pathlib import Path
import sys
print(sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PYREQ
  )
  if [[ -f $stamp ]]; then
    if installed=$(cat "$stamp" 2>/dev/null); then :; else installed=""; fi
  fi
  if [[ $installed == "$digest" ]] \
     && "$TARGET/.venv/bin/python" -c 'import pip' >/dev/null 2>&1; then
    log_step "Состав Python-зависимостей не изменился; установка через pip пропущена"
    return 0
  fi

  log_step "Устанавливаются Python-зависимости из $requirements"
  "$TARGET/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
  "$TARGET/.venv/bin/python" -m pip install --disable-pip-version-check -r "$requirements"
  printf '%s\n' "$digest" > "$stamp"
  chmod 600 "$stamp"
}

# Python, запущенный с путём к скрипту, автоматически видит каталог скрипта.
# При передаче кода через stdin Python видит только текущий каталог установщика.
# Поэтому все проверки stdin/-c с импортом модулей приложения выполняются из TARGET:
# так сохранённый config.py и локальные модули доступны во время обновления.
target_python() {
  (
    cd "$TARGET"
    PYTHONPATH="$TARGET${PYTHONPATH:+:$PYTHONPATH}" exec "$TARGET/.venv/bin/python" "$@"
  )
}

preflight
# An interrupted update may leave a systemd web unit pointing at a missing .venv.
# Disable it temporarily so systemd cannot endlessly restart a known-broken process.
if [[ -n "${UPDATE_PATH:-}" && ! -x "${UPDATE_PATH:-$TARGET}/.venv/bin/python" ]]; then
  systemctl stop vpn-service-web.service vpn-service-web.socket fargovpn-web.service 2>/dev/null || true
  systemctl disable vpn-service-web.service vpn-service-web.socket fargovpn-web.service 2>/dev/null || true
  echo "ℹ Broken previous web service is disabled temporarily; the installer will recreate the virtualenv before enabling it."
fi

write_update_status() {
  local state=$1
  local detail=${2:-}
  local progress=${3:-}
  local phase=${4:-$state}
  [[ -n ${VPN_UPDATE_STATUS_FILE:-} ]] || return 0
  python3 - "$VPN_UPDATE_STATUS_FILE" "$state" "$VERSION" "$detail" "${PROFILE:-unknown}" "$progress" "$phase" "${VPN_UPDATE_JOB_ID:-}" <<'PY'
import fcntl, json, os, pathlib, secrets, sys, threading
from datetime import datetime, timezone
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
lock_path = path.with_name(path.name + ".lock")
with lock_path.open("a+") as lock_handle:
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except Exception:
        previous = {}
    data = {
        **previous,
        "state": sys.argv[2],
        "version": sys.argv[3],
        "detail": sys.argv[4],
        "message": sys.argv[4],
        "profile": sys.argv[5],
        "phase": sys.argv[7],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if sys.argv[6]:
        data["progress"] = max(
            int(previous.get("progress") or 0),
            max(0, min(100, int(sys.argv[6]))),
        )
    if sys.argv[8]:
        data["job_id"] = sys.argv[8]
    if sys.argv[2] in {"completed", "failed"}:
        data["finished_at"] = data["updated_at"]
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
PY
}

cleanup() {
  [[ -z ${PREUPDATE_BACKUP_PART:-} ]] || rm -f -- "$PREUPDATE_BACKUP_PART"
  [[ -z ${MASK_TMP_FILE:-} ]] || rm -f -- "$MASK_TMP_FILE"
  [[ -z $TMP ]] || rm -rf "$TMP"
}

restart_unit_family() {
  local unit
  for unit in "$@"; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      systemctl enable "$unit" >/dev/null 2>&1 || true
      systemctl restart "$unit" >/dev/null 2>&1 || true
      return 0
    fi
  done
  return 0
}

restart_existing_services() {
  systemctl daemon-reload >/dev/null 2>&1 || true
  restart_unit_family vpn-service-bot.service fargovpn-bot.service
  restart_unit_family vpn-service-web.service fargovpn-web.service
  restart_unit_family vpn-service-backup.timer fargovpn-backup.timer
  restart_unit_family vpn-service-reminders.timer fargovpn-reminders.timer
}

on_error() {
  local code=$?
  local line=${1:-неизвестно}
  local command=${2:-неизвестная_команда}
  local rollback_note=''
  trap - ERR
  set +e

  if [[ -n ${PREUPDATE_BACKUP:-} && -f $PREUPDATE_BACKUP \
        && -n ${TARGET:-} && $TARGET == /* && $TARGET != / && ${#TARGET} -gt 5 ]]; then
    write_update_status "rolling-back" "Ошибка установки; восстанавливается предыдущая версия" "${VPN_UPDATE_PROGRESS:-50}" "rollback"
    for unit in vpn-service-bot.service vpn-service-web.service vpn-service-web.socket vpn-service-backup.timer vpn-service-reminders.timer; do
      systemctl stop "$unit" >/dev/null 2>&1 || true
    done
    rm -rf -- "$TARGET"
    mkdir -p "$(dirname "$TARGET")"
    if tar -xzf "$PREUPDATE_BACKUP" -C "$(dirname "$TARGET")"; then
      if [[ -n ${PREUPDATE_SYSTEMD_DIR:-} && -d $PREUPDATE_SYSTEMD_DIR ]]; then
        rm -f \
          /etc/systemd/system/vpn-service-bot.service \
          /etc/systemd/system/vpn-service-web.service \
          /etc/systemd/system/vpn-service-web.socket \
          /etc/systemd/system/vpn-service-backup.service \
          /etc/systemd/system/vpn-service-backup.timer \
          /etc/systemd/system/vpn-service-reminders.service \
          /etc/systemd/system/vpn-service-reminders.timer \
          /etc/systemd/system/vpn-service-update@.service \
          /etc/systemd/system/vpn-service-broadcast@.service
        cp -a "$PREUPDATE_SYSTEMD_DIR"/. /etc/systemd/system/ 2>/dev/null || true
      fi
      restart_existing_services
      rollback_note=" Предыдущая версия и systemd-службы автоматически восстановлены из $PREUPDATE_BACKUP."
    else
      rollback_note=" Автоматический откат из $PREUPDATE_BACKUP не удался; требуется ручное восстановление."
    fi
  else
    # Если файлы ещё не заменялись, например не удалось создать резервную копию,
    # возвращаем неизменённую установку в рабочее состояние, а не оставляем
    # её службы остановленными.
    restart_existing_services
    rollback_note=" Текущие файлы не заменялись; прежние службы запущены повторно."
  fi

  write_update_status "failed" "Ошибка на строке $line: $command.$rollback_note" "${VPN_UPDATE_PROGRESS:-50}" "failed"
  echo "Установка завершилась ошибкой на строке $line: $command.$rollback_note" >&2
  exit "$code"
}

trap cleanup EXIT
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

ask() {
  local name=$1 prompt=$2 default=${3:-} secret=${4:-no} value=''
  while [[ -z $value ]]; do
    if [[ $secret == yes ]]; then
      read -rsp "$prompt${default:+ [$default]}: " value
      echo
    else
      read -rp "$prompt${default:+ [$default]}: " value
    fi
    value=${value:-$default}
  done
  printf -v "$name" '%s' "$value"
}


choose_profile() {
  local choice=''
  cat <<'EOF_PROFILE'
Профиль установки:
1) Полный (full): Telegram-бот и веб-панель администратора
2) Облегчённый (lite): только Telegram-бот, без веб-панели и её зависимостей
EOF_PROFILE
  read -rp 'Выберите профиль [1]: ' choice
  case ${choice:-1} in
    1) PROFILE=full ;;
    2) PROFILE=lite ;;
    *) echo 'Некорректный выбор профиля.' >&2; exit 1 ;;
  esac
}

mask_is_installed() {
  # Detect the Mask by several independent artifacts so an already-installed
  # router is never offered for a second installation.
  local markers=0
  local details=()
  [[ -d /root/nginx_mask_setup ]] && { markers=$((markers+1)); details+=("/root/nginx_mask_setup"); }
  [[ -f /root/nginx_mask_setup/setup_mask.env ]] && { markers=$((markers+1)); details+=("setup_mask.env"); }
  [[ -f /etc/nginx/stream.d/00-stream.conf ]] && { markers=$((markers+1)); details+=("00-stream.conf"); }
  [[ -f /var/log/nginx_mask_install.log ]] && { details+=("nginx_mask_install.log"); }
  if systemctl list-unit-files 2>/dev/null | grep -Eiq 'nginx.*mask|mask.*nginx|setup-mask'; then
    markers=$((markers+1)); details+=("systemd-mask-unit");
  fi
  if [[ -n "${details[*]:-}" ]]; then
    MASK_DETECTION_REASON="${details[*]}"
  else
    MASK_DETECTION_REASON=""
  fi
  # A strong marker is enough: installations may use only the stream config or
  # only the persisted setup directory depending on how the original project
  # was installed. A lone historical log is intentionally not a marker.
  if [[ -f /etc/nginx/stream.d/00-stream.conf || ( -d /root/nginx_mask_setup && -f /root/nginx_mask_setup/setup_mask.env ) || -n "$(systemctl list-unit-files 2>/dev/null | grep -Ei 'nginx.*mask|mask.*nginx|setup-mask' | head -n1)" ]]; then
    return 0
  fi
  return 1
}

mask_metadata_dir() {
  # The official Mask installer may or may not create this directory.
  # FargoVPN owns its metadata files, so create it explicitly before writing.
  install -d -m 700 /root/nginx_mask_setup
}

refresh_mask_metadata() {
  MASK_LATEST_REF=''
  MASK_LATEST_VERSION=''
  local api_json=''
  if ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  api_json=$(curl -fsSL --retry 3 --connect-timeout 8 --max-time 20 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    'https://api.github.com/repos/torrua/Nginx-L4-Stream-Router-Mask-for-3x-ui/commits/main' \
    2>/dev/null || true)
  if [[ -n $api_json ]]; then
    if command -v jq >/dev/null 2>&1; then
      MASK_LATEST_REF=$(printf '%s' "$api_json" | jq -r '.sha // empty' 2>/dev/null | cut -c1-12 || true)
    elif command -v python3 >/dev/null 2>&1; then
      MASK_LATEST_REF=$(printf '%s' "$api_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(str(d.get("sha") or "")[:12])' 2>/dev/null || true)
    else
      MASK_LATEST_REF=$(printf '%s' "$api_json" | sed -nE 's/.*"sha"[[:space:]]*:[[:space:]]*"([0-9a-fA-F]{12,40})".*/\1/p' | cut -c1-12)
    fi
  fi
  MASK_LATEST_VERSION=$(curl -fsSL --retry 2 --connect-timeout 5 --max-time 15 \
    'https://raw.githubusercontent.com/torrua/Nginx-L4-Stream-Router-Mask-for-3x-ui/main/VERSION' \
    2>/dev/null | tr -d '[:space:]' || true)
}

choose_mask() {
  [[ $PROFILE == full ]] || { MASK_MODE=no; return 0; }
  if [[ $MASK_MODE_EXPLICIT -eq 1 && $MASK_MODE == no ]]; then
    echo 'Для полного профиля Nginx L4 Stream Router Mask обязателен. --mask no разрешён только для lite.' >&2
    exit 2
  fi
  echo
  echo '────────────────────────────────────────────────────────────'
  echo 'Проверка Nginx L4 Stream Router Mask для 3x-ui'
  echo 'Этот шаг выполняется до установки/обновления FargoVPN.'
  echo '────────────────────────────────────────────────────────────'
  if mask_is_installed; then
    echo
    echo '✓ Nginx L4 Stream Router Mask уже обнаружен на сервере.'
    echo "  Найдено: ${MASK_DETECTION_REASON:-признаки установленного Mask}."
    MASK_MODE=no
    MASK_ACTION=skip
    MASK_LATEST_REF=''
    MASK_LATEST_VERSION=''
    MASK_INSTALLED_REF=''
    if [[ $MASK_MODE_EXPLICIT -eq 1 && $MASK_MODE == no ]]; then
      echo '  Указано --mask no: обновление Mask пропускается.'
      return 0
    fi
    # Проверяем именно текущую ветку main репозитория GitHub.
    refresh_mask_metadata
    mask_metadata_dir
    if [[ -f /root/nginx_mask_setup/.github_commit ]]; then
      MASK_INSTALLED_REF=$(tr -d '[:space:]' < /root/nginx_mask_setup/.github_commit 2>/dev/null || true)
    fi
    echo
    if [[ -n $MASK_LATEST_REF ]]; then
      echo "  Актуальный commit GitHub (main): $MASK_LATEST_REF"
      [[ -n ${MASK_LATEST_VERSION:-} ]] && echo "  Актуальная версия install.sh: $MASK_LATEST_VERSION"
      if [[ -n $MASK_INSTALLED_REF ]]; then
        echo "  Последний применённый commit:       $MASK_INSTALLED_REF"
        if [[ "$MASK_INSTALLED_REF" == "$MASK_LATEST_REF" ]]; then
          echo '  ✓ Mask соответствует текущему commit GitHub.'
        else
          echo '  ⚠ Установленный Mask отличается от текущего commit GitHub.'
        fi
      else
        echo '  Последний commit установки ещё не сохранён; после обновления он будет записан.'
      fi
    else
      echo '  Актуальный commit GitHub определить не удалось.'
      echo '  Проверка выполнена через GitHub API; если сервер ограничивает исходящий HTTPS, обновление можно запустить вручную:'
      echo '  curl -sSL https://raw.githubusercontent.com/torrua/Nginx-L4-Stream-Router-Mask-for-3x-ui/main/install.sh | sudo bash'
    fi
    if [[ $NONINTERACTIVE -eq 1 && $MASK_MODE_EXPLICIT -eq 0 ]]; then
      if [[ -n $MASK_LATEST_REF && -n $MASK_INSTALLED_REF && $MASK_LATEST_REF != $MASK_INSTALLED_REF ]]; then
        echo '  Неинтерактивный режим: найден более новый GitHub commit, но текущий Mask не перезаписывается автоматически.'
        echo '  Для обновления явно укажите --mask yes или выполните официальный install.sh из GitHub.'
      else
        echo '  Неинтерактивный режим: установленный Mask соответствует GitHub или commit пока не известен.'
      fi
      return 0
    fi
    cat <<'EOF_MASK_UPDATE'

Mask уже установлен. Текущий install.sh берётся непосредственно из GitHub main:
1) Обновить Mask до текущего GitHub install.sh (рекомендуется)
2) Оставить текущую версию
EOF_MASK_UPDATE
    local update_choice=''
    read -rp 'Обновить Nginx L4 Stream Router Mask? [2]: ' update_choice
    case ${update_choice:-2} in
      1) MASK_MODE=yes; MASK_ACTION=update ;;
      2) MASK_MODE=no; MASK_ACTION=skip ;;
      *) echo 'Некорректный выбор.' >&2; exit 1 ;;
    esac
    return 0
  fi
  if [[ $NONINTERACTIVE -eq 1 && $MASK_MODE_EXPLICIT -eq 0 ]]; then
    # Automatic/non-interactive mode on a server without Mask = recommended installation.
    MASK_MODE=yes
    return 0
  fi
  cat <<'EOF_MASK'

Шаг 1. Nginx L4 Stream Router Mask для 3x-ui
----------------------------------------------------------------
Mask лучше установить ДО FargoVPN. Он позволяет оставить 3x-ui и
FargoVPN за единым внешним HTTPS 443 и подготавливает маршрутизацию.

1) Установить Mask (рекомендуется)
2) Пропустить

Установщик скачает официальный install.sh проекта torrua и запустит
его с правами root. После этого FargoVPN продолжит установку автоматически.
EOF_MASK
  local choice=''
  read -rp 'Установить Nginx L4 Stream Router Mask? [1]: ' choice
  case ${choice:-1} in
    1) MASK_MODE=yes ;;
    2) MASK_MODE=no ;;
    *) echo 'Некорректный выбор.' >&2; exit 1 ;;
  esac
}


detect_nginx_socket_group() {
  local nginx_user="" nginx_group=""
  # Prefer the actual configured Nginx worker account. Ubuntu/Debian commonly
  # use www-data, while some packaged builds use nginx.
  nginx_user=$(nginx -T 2>/dev/null | sed -nE 's/^[[:space:]]*user[[:space:]]+([^;[:space:]]+)([[:space:]]+[^;[:space:]]+)?;.*/\1/p' | head -n1 || true)
  if [[ -z "$nginx_user" ]]; then
    nginx_user=$(ps -eo user=,args= 2>/dev/null | sed -nE 's/^([^[:space:]]+)[[:space:]]+nginx: worker process.*/\1/p' | head -n1 || true)
  fi
  [[ -n "$nginx_user" ]] || nginx_user=www-data
  if ! getent passwd "$nginx_user" >/dev/null 2>&1; then
    echo "Не удалось определить пользователя worker Nginx: $nginx_user" >&2
    return 1
  fi
  nginx_group=$(id -gn "$nginx_user")
  [[ -n "$nginx_group" ]] || { echo "Не удалось определить группу worker Nginx." >&2; return 1; }
  printf '%s\n' "$nginx_group"
}

verify_fargovpn_nginx_route() {
  local prefix="$1"
  local socket_path="$2"
  [[ -S "$socket_path" ]] || { echo "FargoVPN socket отсутствует: $socket_path" >&2; return 1; }
  local mode owner group expected_group
  mode=$(stat -c '%a' "$socket_path")
  owner=$(stat -c '%U' "$socket_path")
  group=$(stat -c '%G' "$socket_path")
  expected_group=$(detect_nginx_socket_group) || return 1
  [[ "$owner" == "root" && "$group" == "$expected_group" ]] || {
    echo "Неверный владелец FargoVPN socket: $owner:$group (ожидался root:$expected_group)." >&2
    return 1
  }
  [[ "$mode" == "660" ]] || { echo "Неверные права FargoVPN socket: $mode (ожидалось 660)." >&2; return 1; }
  if [[ -x "${TARGET:-}/.venv/bin/python" && -f "${TARGET:-}/nginx_panel_guard.py" ]]; then
    if ! "${TARGET}/.venv/bin/python" "${TARGET}/nginx_panel_guard.py" --check; then
      echo "FargoVPN location ${prefix}/ не найден в фактически загруженной конфигурации nginx -T." >&2
      return 1
    fi
    if nginx -T 2>&1 | grep -A80 -F "location ^~ ${prefix}/" | grep -Eq 'proxy_(redirect|cookie_path)[[:space:]]'; then
      echo "Найдены нежелательные proxy_redirect/proxy_cookie_path в FargoVPN location." >&2
      return 1
    fi
  else
    local nginx_dump
    nginx_dump=$(nginx -T 2>&1) || return 1
    grep -Fq "location ^~ ${prefix}/" <<<"$nginx_dump" || { echo "FargoVPN location ${prefix}/ не найден в nginx -T." >&2; return 1; }
    grep -Fq "proxy_pass http://unix:${socket_path}:/" <<<"$nginx_dump" || { echo "FargoVPN proxy_pass на Unix socket не найден в nginx -T." >&2; return 1; }
  fi
  return 0
}

verify_nginx_mask_ready() {
  [[ $PROFILE == full ]] || return 0
  log_step 'Проверка готовности Nginx L4 Stream Router Mask перед установкой FargoVPN'

  require_command nginx
  require_command systemctl
  [[ -f /etc/nginx/nginx.conf ]] || {
    echo 'Nginx установлен некорректно: отсутствует /etc/nginx/nginx.conf.' >&2
    return 1
  }
  [[ -f /etc/nginx/stream.d/00-stream.conf ]] || {
    echo 'Nginx L4 Stream Router Mask не установлен: отсутствует /etc/nginx/stream.d/00-stream.conf.' >&2
    return 1
  }
  systemctl daemon-reload
  systemctl enable nginx.service >/dev/null 2>&1
  systemctl start nginx.service
  systemctl is-active --quiet nginx.service || {
    echo 'Служба nginx.service не находится в состоянии active. FargoVPN не устанавливается.' >&2
    journalctl -u nginx.service -n 120 --no-pager || true
    return 1
  }
  nginx -t

  if ! ss -ltnp 2>/dev/null | grep -Eq ':443\b'; then
    echo 'Nginx не слушает TCP 443. L4 Router/Mask не готов, установка FargoVPN остановлена.' >&2
    ss -ltnp 2>/dev/null || true
    return 1
  fi

  local stream_conf='/etc/nginx/stream.d/00-stream.conf'
  grep -Eq '^\s*server\s*\{' "$stream_conf" || {
    echo 'В 00-stream.conf отсутствует server-блок L4.' >&2
    return 1
  }
  grep -Eq 'proxy_pass\s+\$backend_gate|proxy_pass\s+[^;]+' "$stream_conf" || {
    echo 'В 00-stream.conf не найден L4 proxy_pass.' >&2
    return 1
  }
  MASK_READY=1
  echo '✓ Nginx L4 Stream Router Mask полностью готов: nginx active, конфигурация валидна, TCP 443 слушается.'
}

print_mask_summary() {
  echo
  echo '────────────────────────────────────────────────────────────'
  echo 'Nginx L4 Stream Router Mask — итог настройки'
  echo '────────────────────────────────────────────────────────────'
  if [[ -f /root/nginx_mask_setup/.github_version ]]; then
    local v
    v=$(tr -d '[:space:]' < /root/nginx_mask_setup/.github_version 2>/dev/null || true)
    [[ -n $v ]] && echo "Версия Mask: $v"
  fi
  if [[ -f /root/nginx_mask_setup/.github_commit ]]; then
    local c
    c=$(tr -d '[:space:]' < /root/nginx_mask_setup/.github_commit 2>/dev/null || true)
    [[ -n $c ]] && echo "GitHub commit: $c"
  fi
  echo 'Nginx: active'
  echo 'Конфигурация: nginx -t OK'
  if ss -ltnp 2>/dev/null | grep -Eq ':443\b'; then
    echo 'TCP 443: listening'
  else
    echo 'TCP 443: НЕ СЛУШАЕТСЯ'
  fi

  local env_file='/root/nginx_mask_setup/setup_mask.env'
  if [[ -f $env_file ]]; then
    echo
    echo 'Параметры Mask/3x-ui:'
    local key value
    while IFS='=' read -r key value; do
      [[ $key =~ ^[[:space:]]*[A-Z0-9_]+[[:space:]]*$ ]] || continue
      key=${key//[[:space:]]/}
      value=${value%$'\r'}
      value=${value#\"}; value=${value%\"}
      value=${value#\'}; value=${value%\'}
      case "$key" in
        DOMAIN|PRIMARY_DOMAIN|PANEL_DOMAIN|PANEL_PATH|SUB_PATH|SUBSCRIPTION_PATH|VLESS_PATH|XHTTP_PATH|REALITY_PATH|HYSTERIA_PATH|HYSTERIA2_PATH|HTTP_PATH|HTTPS_PATH|PANEL_PORT|INTERNAL_PORT|PUBLIC_PORT|SSH_PORT)
          [[ -n $value ]] && printf '  %-20s %s\n' "$key:" "$value"
          ;;
      esac
    done < "$env_file"
  fi

  echo
  if [[ -f /root/vpn_credentials.txt ]]; then
    echo 'Файл параметров 3x-ui: /root/vpn_credentials.txt'
    echo 'Секреты и пароли намеренно не выводятся в консоль.'
  fi
  echo 'Полный лог Mask: /var/log/nginx_mask_install.log'
  echo '────────────────────────────────────────────────────────────'
}

print_mask_access_info() {
  MASK_ACCESS_PRINTED=1
  local credentials='/root/vpn_credentials.txt'
  echo
  echo '────────────────────────────────────────────────────────────'
  echo 'Доступы и параметры 3X-UI / VPN'
  echo '────────────────────────────────────────────────────────────'

  # The official Mask installer is expected to be synchronous, but the final
  # credential file can be flushed a moment after its last console line. Wait
  # briefly for the actual artifact rather than declaring the Mask unfinished.
  for _ in $(seq 1 30); do
    [[ -s "$credentials" ]] && break
    sleep 1
  done

  if [[ -s "$credentials" ]]; then
    echo 'Данные, сформированные официальным установщиком Mask:'
    echo
    cat "$credentials"
    echo
    echo "Файл с этими данными: $credentials"
  else
    echo 'Файл /root/vpn_credentials.txt не найден.'
    echo 'Итоговый вывод официального Mask (последние 80 строк):'
    if [[ -n ${MASK_OUTPUT_FILE:-} && -f ${MASK_OUTPUT_FILE:-} ]]; then
      tail -n 80 "$MASK_OUTPUT_FILE"
    elif [[ -f /var/log/nginx_mask_install.log ]]; then
      tail -n 80 /var/log/nginx_mask_install.log
    else
      echo 'Лог Mask не найден.'
    fi
  fi

  if systemctl is-active --quiet x-ui.service 2>/dev/null || systemctl is-active --quiet x-ui 2>/dev/null; then
    echo
    echo '3X-UI: active'
  elif systemctl list-unit-files 2>/dev/null | grep -q '^x-ui\.service'; then
    echo
    echo '3X-UI: НЕ active'
    systemctl status x-ui.service --no-pager -n 20 2>/dev/null || true
  fi
  echo '────────────────────────────────────────────────────────────'
  rm -f "${MASK_OUTPUT_FILE:-}" 2>/dev/null || true
  MASK_OUTPUT_FILE=''
}

install_mask_router() {
  [[ $MASK_MODE == yes ]] || return 0
  require_command curl
  if [[ ${MASK_ACTION:-install} == update ]]; then
    log_step 'Шаг 1/2: обновление Nginx L4 Stream Router Mask до текущего GitHub install.sh'
  else
    log_step 'Шаг 1/2: установка Nginx L4 Stream Router Mask для 3x-ui'
  fi
  local mask_url='https://raw.githubusercontent.com/torrua/Nginx-L4-Stream-Router-Mask-for-3x-ui/main/install.sh'
  local mask_tmp
  mask_tmp=$(mktemp /tmp/fargovpn-mask.XXXXXX.sh)
  MASK_TMP_FILE="$mask_tmp"
  curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 "$mask_url" -o "$mask_tmp"
  chmod 700 "$mask_tmp"
  local mask_output
  mask_output=$(mktemp /tmp/fargovpn-mask-output.XXXXXX.log)
  if ! bash "$mask_tmp" 2>&1 | tee -a "$INSTALL_LOG" | tee "$mask_output"; then
    echo 'Установка Nginx L4 Stream Router Mask завершилась ошибкой.' >&2
    echo 'Проверьте /var/log/nginx_mask_install.log и /tmp/setup_mask_cmd.log.' >&2
    rm -f "$mask_output" "$mask_tmp"
    exit 1
  fi
  MASK_OUTPUT_FILE="$mask_output"
  # Refresh metadata after the official installer finishes; it may create or remove
  # its own helper directory, so FargoVPN recreates the metadata directory explicitly.
  refresh_mask_metadata
  mask_metadata_dir
  if [[ -n ${MASK_LATEST_REF:-} ]]; then
    printf '%s\n' "$MASK_LATEST_REF" > /root/nginx_mask_setup/.github_commit
    chmod 600 /root/nginx_mask_setup/.github_commit 2>/dev/null || true
  fi
  if [[ -n ${MASK_LATEST_VERSION:-} ]]; then
    printf '%s\n' "$MASK_LATEST_VERSION" > /root/nginx_mask_setup/.github_version
    chmod 600 /root/nginx_mask_setup/.github_version 2>/dev/null || true
  fi
  rm -f "$mask_tmp"
  MASK_TMP_FILE=''
  if [[ ${MASK_ACTION:-install} == update ]]; then
    log_step 'Nginx L4 Stream Router Mask обновлён.'
  else
    log_step 'Nginx L4 Stream Router Mask установлен.'
  fi
  # Do not continue silently: show the resulting Mask/3x-ui parameters before
  # FargoVPN is touched. This is especially important because the official
  # installer writes part of its detailed output to its own log.
  verify_nginx_mask_ready
  print_mask_summary
  print_mask_access_info
  log_step 'Переходим к установке FargoVPN.'
}

project_dir() {
  local path=${1%/}
  if [[ -f "$path/main.py" && -f "$path/config.py" ]]; then
    realpath -m "$path"
    return 0
  fi
  if [[ -f "$path/app/main.py" && -f "$path/app/config.py" ]]; then
    realpath -m "$path/app"
    return 0
  fi
  return 1
}

config_literal() {
  local path=$1 name=$2
  python3 - "$path" "$name" <<'PY'
import ast, pathlib, sys
path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, bool):
                print("true" if value else "false")
            elif isinstance(value, (str, int, float)):
                print(value)
            break
except Exception:
    pass
PY
}

config_is_valid() {
  local path=$1
  [[ -f "$path" ]] || return 1
  python3 - "$path" <<'PYCV'
import pathlib, sys
p=pathlib.Path(sys.argv[1])
try:
    compile(p.read_text(encoding="utf-8"), str(p), "exec")
except Exception:
    raise SystemExit(1)
PYCV
}

recover_config_file() {
  local source=$1 destination=$2 template=${3:-$SRC/config.example.py}
  python3 - "$source" "$destination" "$template" <<'PYREC'
import ast, pathlib, re, sys
source, destination, template = map(pathlib.Path, sys.argv[1:4])
raw = source.read_text(encoding="utf-8", errors="replace")
values = {}
for line in raw.splitlines():
    m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
    if not m:
        continue
    name, expr = m.groups()
    try:
        values[name] = ast.literal_eval(expr)
    except Exception:
        continue
if not values:
    raise SystemExit("Не удалось извлечь ни одного безопасного параметра из повреждённого config.py")
base = template.read_text(encoding="utf-8") if template.is_file() else "import aiohttp\n"
for name, value in values.items():
    rendered = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    if re.search(pattern, base):
        base = re.sub(pattern, rendered, base)
    else:
        base = base.rstrip() + "\n" + rendered + "\n"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(base.rstrip() + "\n", encoding="utf-8")
compile(base, str(destination), "exec")
PYREC
}

find_old() {
  local paths=() path normalized unit
  for unit in fargovpn-bot fargovpn-web vpn-service-bot vpn-service-web; do
    if path=$(systemctl show -p WorkingDirectory --value "$unit.service" 2>/dev/null); then :; else path=""; fi
    [[ -z $path ]] || paths+=("$path")
  done
  paths+=("/root/vpn_bot" "/opt/vpn-service/app" "/opt/vpn-service" "/opt/fargovpn" "/opt/fargovpn-bot" "/root/FargoVPN_Bot" "/srv/vpn-bot")
  for path in "${paths[@]}"; do
    if normalized=$(project_dir "$path" 2>/dev/null); then :; else normalized=""; fi
    [[ -z $normalized ]] || printf '%s\n' "$normalized"
  done | awk 'NF && !seen[$0]++'
}


repair_nginx() {
  local target="$1"
  [[ -n "$target" && -f "$target/config.py" && -f "$target/nginx_panel_guard.py" ]] || {
    echo "Не найдено существующее приложение FargoVPN: $target" >&2
    return 2
  }
  if [[ ! -x "$target/.venv/bin/python" ]]; then
    echo "Не найден Python venv: $target/.venv/bin/python" >&2
    return 2
  fi
  echo "Восстановление Nginx-маршрута FargoVPN..."
  if ! "$target/.venv/bin/python" "$target/nginx_panel_guard.py" --once; then
    echo "Не удалось автоматически восстановить FargoVPN location в подключённой конфигурации Nginx." >&2
    nginx -T 2>&1 | grep -nE 'server_name|listen .*443|fargovpn-admin' | head -n 160 >&2 || true
    return 1
  fi
  nginx -t
  systemctl reload nginx
  systemctl restart vpn-service-nginx-guard.service 2>/dev/null || true
  SOCKET_PATH=$("$target/.venv/bin/python" -c 'import config; print(str(getattr(config,"WEB_SOCKET_PATH","/run/vpn-service/fargovpn.sock")))')
  [[ -S "$SOCKET_PATH" ]] || { echo "Unix socket не найден: $SOCKET_PATH" >&2; return 1; }
  curl --unix-socket "$SOCKET_PATH" -fsS http://localhost/health >/tmp/fargovpn-repair-health.txt
  cat /tmp/fargovpn-repair-health.txt
  echo
  echo "Nginx-маршрут восстановлен."
}

if [[ "${1:-}" == "--repair-nginx" ]]; then
  TARGET_REPAIR="${2:-/root/vpn_bot}"
  repair_nginx "$TARGET_REPAIR"
  exit $?
fi

if [[ -n $UPDATE_PATH ]]; then
  if OLD=$(project_dir "$UPDATE_PATH"); then :; else OLD=""; fi
  [[ -n $OLD ]] || { echo 'В указанной установке не найдены main.py и config.py.' >&2; exit 2; }
  TARGET="$OLD"
  MODE=2
  # Explicit --update-existing is intentionally non-interactive. The menu-based
  # update path below remains interactive so the Mask preflight can be shown.
  NONINTERACTIVE=1
else
  cat <<EOF_MENU
========================================
 VPN Service Platform $VERSION
========================================
Каталог приложения по умолчанию: $DEFAULT_TARGET
1) Новая установка
2) Обновление существующей установки
3) Восстановление конфигурации и баз данных из резервной копии
EOF_MENU
  read -rp 'Выберите вариант: ' MODE
  [[ $MODE =~ ^[123]$ ]] || { echo 'Некорректный выбор.' >&2; exit 1; }
  if [[ $MODE == 2 ]]; then
    mapfile -t LIST < <(find_old)
    if ((${#LIST[@]})); then
      echo 'Найдены установки:'
      for index in "${!LIST[@]}"; do echo "$((index + 1))) ${LIST[$index]}"; done
      echo "$(( ${#LIST[@]} + 1 ))) Указать путь вручную"
      read -rp 'Выберите вариант: ' number
      if [[ $number =~ ^[0-9]+$ ]] && ((number >= 1 && number <= ${#LIST[@]})); then
        OLD=${LIST[$((number - 1))]}
      else
        ask MANUAL 'Каталог существующей установки'
        if OLD=$(project_dir "$MANUAL"); then :; else OLD=""; fi
      fi
    else
      ask MANUAL 'Каталог существующей установки'
      if OLD=$(project_dir "$MANUAL"); then :; else OLD=""; fi
    fi
    [[ -n $OLD ]] || { echo 'Существующая установка не найдена.' >&2; exit 1; }
    TARGET="$OLD"
  elif [[ $MODE == 3 ]]; then
    ask RESTORE 'Путь к резервной копии .tar.gz'
    [[ -f $RESTORE ]] || { echo 'Архив резервной копии не найден.' >&2; exit 1; }
  fi
fi

if [[ -n $OLD && -f $OLD/config.py ]]; then
  detected_backup=$(config_literal "$OLD/config.py" BACKUP_DIR)
  [[ -z $detected_backup || $detected_backup != /* ]] || BACKUP=$detected_backup
  if [[ -z $PROFILE ]]; then
    detected_profile=$(config_literal "$OLD/config.py" INSTALL_PROFILE)
    case ${detected_profile,,} in
      full|lite) PROFILE=${detected_profile,,} ;;
    esac
  fi
fi

if [[ -z $PROFILE ]]; then
  if [[ $MODE == 2 && $NONINTERACTIVE -eq 1 ]]; then
    # В версиях до 2.4.0 не было маркера профиля; по умолчанию использовался полный профиль.
    PROFILE=full
  else
    choose_profile
  fi
fi

[[ $PROFILE == full || $PROFILE == lite ]] || { echo 'Некорректный профиль установки.' >&2; exit 2; }
if [[ $PROFILE == full ]]; then
  [[ -f "$SRC/webapp.py" && -f "$SRC/update_manager.py" && -f "$SRC/requirements.txt" ]] || {
    echo 'Этот архив содержит облегчённую сборку и не может установить полный профиль с веб-панелью.' >&2
    exit 2
  }
else
  [[ -f "$SRC/requirements-lite.txt" ]] || { echo 'Не найден файл requirements-lite.txt.' >&2; exit 2; }
fi

choose_mask
# Full profile has a hard dependency on the L4 Mask. The Mask is installed
# and validated before any FargoVPN application files or services are deployed.
if [[ $PROFILE == full ]]; then
  apt_install curl ca-certificates iproute2 openssl socat
  install_mask_router
  verify_nginx_mask_ready
fi

VPN_UPDATE_PROGRESS=52
write_update_status "installing" "Проверяются системные пакеты" "$VPN_UPDATE_PROGRESS" "dependencies"
export DEBIAN_FRONTEND=noninteractive
if command -v debconf-set-selections >/dev/null 2>&1; then
  echo 'davfs2 davfs2/suid_file boolean false' | debconf-set-selections || true
fi
apt_install python3 python3-venv python3-pip curl sqlite3 rclone davfs2 ca-certificates iproute2 openssl rsync socat tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng certbot
require_command rsync
require_command curl
require_command ss
VPN_UPDATE_PROGRESS=58
write_update_status "installing" "Системные зависимости готовы" "$VPN_UPDATE_PROGRESS" "dependencies"
mkdir -p "$BACKUP" "$(dirname "$TARGET")" /var/lib/vpn-service/updates /var/lib/vpn-service/broadcasts /var/cache/vpn-service/chat-media /var/lib/vpn-service
chmod 700 /var/lib/vpn-service/updates /var/lib/vpn-service/broadcasts /var/cache/vpn-service/chat-media

VPN_UPDATE_PROGRESS=60
write_update_status "installing" "Останавливаются службы перед безопасной заменой файлов" "$VPN_UPDATE_PROGRESS" "stop-services"
for unit in \
  fargovpn-bot fargovpn-web fargovpn-backup fargovpn-reminders \
  vpn-service-bot vpn-service-web vpn-service-nginx-guard vpn-service-backup.service vpn-service-backup.timer \
  vpn-service-reminders.service vpn-service-reminders.timer; do
  # SSE/WebSocket-подобные долгоживущие HTTP-соединения могут удерживать
  # Uvicorn при graceful shutdown. Во время обновления это недопустимо:
  # веб-служба не должна блокировать замену файлов десятки секунд.
  if [[ "$unit" == "vpn-service-web" || "$unit" == "vpn-service-web.service" || "$unit" == "fargovpn-web" || "$unit" == "fargovpn-web.service" ]]; then
    if command -v timeout >/dev/null 2>&1; then
      timeout 6s systemctl stop "$unit" 2>/dev/null || {
        systemctl kill "$unit" --kill-who=all --signal=SIGKILL 2>/dev/null || true
        sleep 1
      }
    else
      systemctl stop "$unit" 2>/dev/null || true
      systemctl kill "$unit" --kill-who=all --signal=SIGKILL 2>/dev/null || true
    fi
  else
    systemctl stop "$unit" 2>/dev/null || true
  fi
done
VPN_UPDATE_PROGRESS=62
write_update_status "installing" "Службы остановлены; подготавливается резервная копия" "$VPN_UPDATE_PROGRESS" "stop-services"

TMP=$(mktemp -d)
PREUPDATE_SYSTEMD_DIR="$TMP/systemd"
mkdir -p "$PREUPDATE_SYSTEMD_DIR"
for unit_file in \
  vpn-service-bot.service vpn-service-web.service \
  vpn-service-backup.service vpn-service-backup.timer \
  vpn-service-reminders.service vpn-service-reminders.timer \
  fargovpn-bot.service fargovpn-web.service \
  fargovpn-backup.service fargovpn-backup.timer \
  fargovpn-reminders.service fargovpn-reminders.timer; do
  [[ ! -f /etc/systemd/system/$unit_file ]] || cp -a "/etc/systemd/system/$unit_file" "$PREUPDATE_SYSTEMD_DIR/"
done
VPN_UPDATE_PROGRESS=64
write_update_status "installing" "Создаётся резервная копия текущей установки" "$VPN_UPDATE_PROGRESS" "backup"
if [[ -n $OLD ]]; then
  stamp=$(date +%Y%m%d_%H%M%S)
  completed_backup="$BACKUP/pre_update_${stamp}.tar.gz"
  PREUPDATE_BACKUP_PART="${completed_backup}.part"
  rm -f -- "$PREUPDATE_BACKUP_PART"
  tar -czf "$PREUPDATE_BACKUP_PART" \
    --exclude="$(basename "$OLD")/.venv" \
    --exclude="$(basename "$OLD")/__pycache__" \
    --exclude="$(basename "$OLD")/*/__pycache__" \
    --exclude="$(basename "$OLD")/*.pyc" \
    --exclude="$(basename "$OLD")/.git" \
    -C "$(dirname "$OLD")" "$(basename "$OLD")"
  tar -tzf "$PREUPDATE_BACKUP_PART" >/dev/null
  chmod 600 "$PREUPDATE_BACKUP_PART"
  mv -f -- "$PREUPDATE_BACKUP_PART" "$completed_backup"
  PREUPDATE_BACKUP_PART=""
  PREUPDATE_BACKUP="$completed_backup"
  systemd_backup="${completed_backup%.tar.gz}.systemd.tar.gz"
  systemd_part="${systemd_backup}.part"
  rm -f -- "$systemd_part"
  systemd_files=()
  while IFS= read -r -d '' unit_file; do
    systemd_files+=("$(basename "$unit_file")")
  done < <(find "$PREUPDATE_SYSTEMD_DIR" -maxdepth 1 -type f -print0)
  if ((${#systemd_files[@]})); then
    tar -czf "$systemd_part" -C "$PREUPDATE_SYSTEMD_DIR" "${systemd_files[@]}"
    tar -tzf "$systemd_part" >/dev/null
    chmod 600 "$systemd_part"
    mv -f -- "$systemd_part" "$systemd_backup"
  fi
  if config_is_valid "$OLD/config.py"; then
    cp "$OLD/config.py" "$TMP/config.py"
  else
    cp "$OLD/config.py" "$TMP/corrupt_config.py"
    if recover_config_file "$OLD/config.py" "$TMP/config.py"; then
      echo "⚠ Старый config.py повреждён; безопасно восстановлены распознаваемые параметры."
    else
      rm -f "$TMP/config.py"
      echo "⚠ Старый config.py повреждён и не поддаётся безопасному восстановлению; будет создан новый конфиг." >&2
    fi
  fi
  for database_file in "$OLD/vpn_bot.db" "$OLD/data/vpn_bot.db"; do
    if [[ -f $database_file ]]; then
      cp "$database_file" "$TMP/vpn_bot.db"
      break
    fi
  done
fi
VPN_UPDATE_PROGRESS=68
write_update_status "installing" "Резервная копия текущей установки проверена" "$VPN_UPDATE_PROGRESS" "backup"

if [[ -n $RESTORE ]]; then
  export RESTORE TMP
  python3 <<'PY'
import os, pathlib, shutil, tarfile
archive_path = pathlib.Path(os.environ["RESTORE"])
destination = pathlib.Path(os.environ["TMP"])
root = destination.resolve()
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        pure = pathlib.PurePosixPath(name)
        if (
            not name
            or member.issym()
            or member.islnk()
            or member.isdev()
            or not (member.isfile() or member.isdir())
            or name.startswith("/")
            or ".." in pure.parts
        ):
            raise SystemExit("Небезопасная структура архива резервной копии")
        target = (destination / name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit("Небезопасный путь в архиве резервной копии")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit("Не удалось прочитать файл из резервной копии")
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        os.chmod(target, member.mode & 0o777)
PY
  for candidate in \
    "$TMP/vpn_service_backup/bot/config.py" \
    "$TMP/vpn_service_backup/config.py" \
    "$TMP/config.py"; do
    [[ -f $candidate ]] || continue
    if config_is_valid "$candidate"; then
      cp "$candidate" "$TMP/restored_config.py"
    elif recover_config_file "$candidate" "$TMP/restored_config.py"; then
      echo "⚠ Конфигурация в резервной копии повреждена; извлечены только безопасные параметры."
    fi
    break
  done
  for candidate in \
    "$TMP/vpn_service_backup/databases/vpn_bot.db" \
    "$TMP/vpn_service_backup/bot/data/vpn_bot.db" \
    "$TMP/vpn_service_backup/vpn_bot.db"; do
    [[ -f $candidate ]] && { cp "$candidate" "$TMP/restored_vpn_bot.db"; break; }
  done
fi

VPN_UPDATE_PROGRESS=70
write_update_status "installing" "Копируются файлы новой версии" "$VPN_UPDATE_PROGRESS" "files"
mkdir -p "$TARGET/data"
rsync -a --checksum --delete \
  --exclude='config.py' \
  --exclude='data/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  "$SRC/" "$TARGET/"
# Persist the changelog of the release that is actually installed. This keeps it
# available on the Updates page even after the release archive is removed.
release_notes_source="$PACKAGE_ROOT/RELEASE_NOTES_${VERSION}.md"
if [[ -f "$release_notes_source" ]]; then
  cp -f "$release_notes_source" "$TARGET/INSTALLED_CHANGELOG.md"
  printf '%s\n' "$VERSION" > "$TARGET/INSTALLED_CHANGELOG_VERSION"
  chmod 600 "$TARGET/INSTALLED_CHANGELOG.md" "$TARGET/INSTALLED_CHANGELOG_VERSION"
fi
find "$TARGET" -type d -name __pycache__ -prune -exec rm -rf {} +
VPN_UPDATE_PROGRESS=73
write_update_status "installing" "Файлы новой версии скопированы и сверены" "$VPN_UPDATE_PROGRESS" "files"

[[ -f $TMP/config.py ]] && cp "$TMP/config.py" "$TARGET/config.py"
[[ -f $TMP/vpn_bot.db ]] && cp "$TMP/vpn_bot.db" "$TARGET/data/vpn_bot.db"
[[ -f $TMP/restored_config.py ]] && cp "$TMP/restored_config.py" "$TARGET/config.py"
[[ -f $TMP/restored_vpn_bot.db ]] && cp "$TMP/restored_vpn_bot.db" "$TARGET/data/vpn_bot.db"

if [[ $PROFILE == lite ]]; then
  rm -f "$TARGET/webapp.py" "$TARGET/update_manager.py" "$TARGET/requirements.txt"
fi

VPN_UPDATE_PROGRESS=75
write_update_status "installing" "Проверяется Python-окружение" "$VPN_UPDATE_PROGRESS" "python"
if [[ ! -x "$TARGET/.venv/bin/python" ]] \
   || ! "$TARGET/.venv/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  log_step "Создаётся новое виртуальное окружение Python"
  rm -rf "$TARGET/.venv"
  python3 -m venv "$TARGET/.venv"
else
  log_step "Существующее виртуальное окружение Python исправно и будет использовано повторно"
fi
if [[ $PROFILE == full ]]; then
  REQUIREMENTS="$TARGET/requirements.txt"
else
  REQUIREMENTS="$TARGET/requirements-lite.txt"
fi
pip_install "$REQUIREMENTS"
VPN_UPDATE_PROGRESS=83
write_update_status "installing" "Python-зависимости установлены" "$VPN_UPDATE_PROGRESS" "python"
command -v tesseract >/dev/null
tesseract --list-langs 2>/dev/null | grep -qx 'rus'
tesseract --list-langs 2>/dev/null | grep -qx 'eng'

CONFIG_FORCE_REBUILD=0
if [[ -f "$TARGET/config.py" ]] && ! config_is_valid "$TARGET/config.py"; then
  cp "$TARGET/config.py" "$TARGET/config.py.corrupt.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
  rm -f "$TARGET/config.py"
  CONFIG_FORCE_REBUILD=1
  echo '⚠ Обнаружен повреждённый config.py. Исходник сохранён отдельно; будет создан новый корректный Python-конфиг.' >&2
fi
if [[ ! -f "$TARGET/config.py" || $CONFIG_FORCE_REBUILD -eq 1 ]]; then
  [[ $NONINTERACTIVE -eq 0 ]] || { echo 'При неинтерактивном обновлении не найден корректный config.py и безопасное восстановление невозможно.' >&2; exit 1; }
  echo
  echo '--- Первичная настройка ---'
  echo 'Обязательны только название сервиса и данные для входа в веб-панель.'
  echo 'Telegram, 3x-ui, оплата и GitHub можно оставить пустыми и заполнить после установки в разделе «Настройки».'
  echo
  ask SERVICE_NAME 'Название VPN-сервиса' 'FargoVPN'
  read -r -s -p 'Токен Telegram-бота (Enter — настроить позже в панели): ' BOT_TOKEN; echo
  read -r -p 'Telegram ID администраторов через запятую (Enter — настроить позже): ' ADMIN_IDS
  read -r -p 'Адрес 3x-ui (Enter — настроить позже): ' BASE_URL
  read -r -s -p 'API-токен 3x-ui (Enter — настроить позже): ' API_TOKEN; echo
  read -r -p 'Базовый URL подписок (Enter — настроить позже): ' SUB_URL
  PRICE=150
  PHONE=''
  BANK=''
  RECEIVER=''

  WEB_USER=admin
  HASH=''
  SECRET=$(openssl rand -hex 32)
  if [[ $PROFILE == full ]]; then
    ask WEB_USER 'Логин веб-панели' 'admin'
    ask WEB_PASS 'Пароль веб-панели' '' yes
    HASH=$("$TARGET/.venv/bin/python" - "$WEB_PASS" <<'PY'
import hashlib, secrets, sys
salt = secrets.token_bytes(16)
iterations = 260000
value = hashlib.pbkdf2_hmac("sha256", sys.argv[1].encode(), salt, iterations).hex()
print(f"pbkdf2_sha256${iterations}${salt.hex()}${value}")
PY
    )
  fi

  read -r -p 'Владелец GitHub-репозитория (Enter — настроить позже): ' GITHUB_OWNER
  read -r -p 'Имя GitHub-репозитория [FargoVPN]: ' GITHUB_REPO; GITHUB_REPO=${GITHUB_REPO:-FargoVPN}
  GITHUB_TOKEN=''
  export TARGET BACKUP PROFILE SERVICE_NAME BOT_TOKEN ADMIN_IDS BASE_URL API_TOKEN SUB_URL PRICE PHONE BANK RECEIVER WEB_USER HASH SECRET GITHUB_OWNER GITHUB_REPO GITHUB_TOKEN
  "$TARGET/.venv/bin/python" <<'PY'
from pathlib import Path
import os
profile = os.environ["PROFILE"]
target = Path(os.environ["TARGET"])
try:
    admin_ids = sorted({int(v.strip()) for v in os.environ.get("ADMIN_IDS", "").replace(";", ",").replace(" ", ",").split(",") if v.strip()})
except ValueError:
    raise SystemExit("ADMIN_IDS должен содержать только числовые Telegram ID через запятую")
values = {
    "INSTALL_PROFILE": profile,
    "SERVICE_NAME": os.environ["SERVICE_NAME"], "BOT_TOKEN": os.environ["BOT_TOKEN"], "ADMIN_IDS": admin_ids,
    "SUBSCRIPTION_DAYS": 30, "BOT_WELCOME_TEXT": "", "BOT_SUPPORT_PROMPT": "",
    "FAQ_INCY_URL": "https://apps.apple.com/ru/app/incy/id6756943388", "BOT_IDENTITY_REFRESH_SECONDS": 600,
    "BOT_SYNC_INTERVAL_SECONDS": 3600, "DB_PATH": str(target / "data/vpn_bot.db"),
    "BASE_URL": os.environ["BASE_URL"], "MASTER_API_URL": os.environ["BASE_URL"], "MASTER_API_TOKEN": os.environ["API_TOKEN"],
    "SUB_BASE_URL": os.environ["SUB_URL"], "PAYMENT_PRICE": int(os.environ["PRICE"]),
    "PAYMENT_PHONE": os.environ["PHONE"], "PAYMENT_BANK": os.environ["BANK"], "PAYMENT_RECEIVER": os.environ["RECEIVER"],
    "RECEIPT_OCR_ENABLED": True, "RECEIPT_AUTO_APPROVE": True, "RECEIPT_MIN_AMOUNT": 150.0, "RECEIPT_MAX_AGE_HOURS": 24,
    "RECEIPT_RECEIVER_ALIASES": "", "RECEIPT_ALLOW_MASKED_PHONE": True, "RECEIPT_TIMEZONE": "Asia/Almaty",
    "RECEIPT_OCR_LANGUAGES": "rus+eng", "RECEIPT_OCR_TIMEOUT": 20, "RECEIPT_FILTER_NAME": True,
    "RECEIPT_FILTER_PHONE": False, "RECEIPT_FILTER_AMOUNT": True, "RECEIPT_FILTER_DATE": False,
    "RECEIPT_FILTER_STATUS": True, "RECEIPT_FILTER_DUPLICATE": True,
    "WEB_HOST": "127.0.0.1", "WEB_SOCKET_PATH": "/run/vpn-service/fargovpn.sock", "WEB_REVERSE_PROXY": True,
    "APP_LOG_PATH": "/var/log/vpn_bot.log",
    "WEB_PUBLIC_PREFIX": os.environ.get("WEB_PUBLIC_PREFIX", ""), "WEB_DOMAIN": "", "WEB_TLS_SERVER_NAME": "",
    "WEB_USERNAME": os.environ["WEB_USER"], "WEB_PASSWORD_HASH": os.environ["HASH"], "WEB_SECRET_KEY": os.environ["SECRET"],
    "WEB_COOKIE_HTTPS_ONLY": True, "WEB_SESSION_MAX_AGE_SECONDS": 28800, "BOT_PANEL_URL": os.environ.get("BOT_PANEL_URL", ""),
    "PUBLIC_PANEL_URL": os.environ.get("BOT_PANEL_URL", ""), "CABINET_ENABLED": True, "CABINET_PATH": "/cabinet",
    "CABINET_SESSION_MAX_AGE_SECONDS": 86400, "WEB_TRUST_PROXY_HEADERS": True,
    "WEB_LOGIN_MAX_ATTEMPTS": 5, "WEB_LOGIN_WINDOW_SECONDS": 900, "WEB_LOGIN_BLOCK_SECONDS": 900,
    "WEB_LOGIN_MAX_BLOCK_SECONDS": 86400, "WEB_LOGIN_SECURITY_RETENTION_DAYS": 30,
    "XUI_DB_PATH": "/etc/x-ui/x-ui.db", "XUI_PANEL_URL": os.environ["BASE_URL"].rstrip("/"),
    "XUI_INTERNAL_BASE_URL": "", "XUI_INTERNAL_AUTO_DETECT": True, "XUI_CACHE_SECONDS": 30, "XUI_VERIFY_TLS": False,
    "REMINDER_DAYS": [7,3,1,0], "REMINDER_LOCK_PATH": "/run/vpn-service-reminders.lock", "METRICS_STORE_INTERVAL_SECONDS": 60,
    "IDENTITY_IMPORT_MAX_MB": 512, "USER_EVENT_KEEP_DAYS": 365, "USER_EVENT_MAX_ROWS": 250000,
    "CHAT_MEDIA_MAX_MB": 100, "CHAT_MEDIA_CACHE_DIR": "/var/cache/vpn-service/chat-media", "CHAT_MEDIA_CACHE_DAYS": 30,
    "CHAT_MEDIA_CACHE_MAX_MB": 512, "BROADCAST_DIR": "/var/lib/vpn-service/broadcasts", "BROADCAST_MEDIA_MAX_MB": 45,
    "BROADCAST_SEND_DELAY_SECONDS": 0.04, "BROADCAST_STALE_SECONDS": 7200,
    "BACKUP_DIR": os.environ["BACKUP"], "BACKUP_KEEP_DAYS": 14, "BACKUP_INTERVAL_DAYS": 3,
    "BACKUP_RETRY_INTERVAL_SECONDS": 900, "BACKUP_PENDING_KEEP_DAYS": 30, "BACKUP_TELEGRAM": True,
    "BACKUP_TELEGRAM_PART_MB": 45, "BACKUP_INCLUDE_VENV": True, "BACKUP_LOCK_PATH": "/run/vpn-service-backup.lock",
    "BACKUP_STATE_PATH": "/var/lib/vpn-service/backup-state.json", "YANDEX_DISK_ENABLED": False, "YANDEX_DISK_MODE": "oauth",
    "YANDEX_DISK_TOKEN": "", "YANDEX_DISK_PATH": "VPN-Service-Backups", "YANDEX_LOCAL_PATH": "/mnt/yandex-disk",
    "YANDEX_LOCAL_REQUIRE_MOUNT": True, "YANDEX_WEBDAV_URL": "https://webdav.yandex.ru", "YANDEX_DAVFS_SECRETS_PATH": "/etc/davfs2/secrets",
    "YANDEX_UPLOAD_RETRIES": 3, "YANDEX_UPLOAD_VERIFY_ATTEMPTS": 8, "YANDEX_UPLOAD_VERIFY_DELAY": 1.5,
    "RCLONE_REMOTE": "", "RCLONE_PATH": "VPN-Service-Backups", "UPDATE_DIR": "/var/lib/vpn-service/updates",
    "UPDATE_PUBLISHER_USERNAME": os.environ["WEB_USER"], "UPDATE_IS_PUBLISHER": profile == "full",
    "GITHUB_API_BASE_URL": "https://api.github.com", "GITHUB_API_TOKEN": os.environ["GITHUB_TOKEN"],
    "GITHUB_REPOSITORY_OWNER": os.environ["GITHUB_OWNER"], "GITHUB_REPOSITORY_NAME": os.environ["GITHUB_REPO"],
    "GITHUB_TARGET_BRANCH": "main", "GITHUB_RELEASE_TAG_PREFIX": "FargoVPN-",
    "GITHUB_RELEASE_NAME_TEMPLATE": "FargoVPN {version}", "GITHUB_RELEASE_ASSET_NAME": "VPN_Service_Platform_{version}_FULL.tar.gz",
    "GITHUB_RELEASE_MAKE_LATEST": True, "GITHUB_RELEASE_DRAFT": False, "GITHUB_RELEASE_PRERELEASE": False,
    "UPDATE_CHECK_INTERVAL": 60, "UPDATE_VERIFY_TLS": True, "UPDATE_MAX_ARCHIVE_MB": 1024, "UPDATE_STALE_JOB_SECONDS": 7200,
    "PUSH_ENABLED": True, "PUSH_VAPID_SUBJECT": "", "PUSH_VAPID_PRIVATE_KEY_PATH": "/var/lib/vpn-service/vapid_private.pem",
    "PUSH_VAPID_PUBLIC_KEY": "", "PUSH_TTL_SECONDS": 3600, "PUSH_MAX_SUBSCRIPTIONS_PER_USER": 8, "PUSH_TEST_ENABLED": True,
}
content = "import aiohttp\n" + "\n".join(f"{k} = {v!r}" for k,v in values.items()) + "\n"
compile(content, str(target / "config.py"), "exec")
target.joinpath("data").mkdir(parents=True, exist_ok=True)
target.joinpath("config.py").write_text(content, encoding="utf-8")
PY
fi

export TARGET BACKUP PROFILE
"$TARGET/.venv/bin/python" <<'PY'
from pathlib import Path
import ast, hashlib, os, re, secrets, sys
path = Path(os.environ["TARGET"]) / "config.py"
text = path.read_text(encoding="utf-8")

def set_value(name, value):
    global text
    line = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    text = re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"

def ensure(name, value):
    if not re.search(rf"(?m)^{re.escape(name)}\s*=", text):
        set_value(name, value)

def literal(name, default=None):
    try:
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
    except Exception:
        return default
    return default

set_value("INSTALL_PROFILE", os.environ["PROFILE"])
set_value("DB_PATH", str(Path(os.environ["TARGET"]) / "data/vpn_bot.db"))
ensure("PUBLIC_PANEL_URL", str(literal("BOT_PANEL_URL", "") or ""))
set_value("BACKUP_DIR", os.environ["BACKUP"])
ensure("SERVICE_NAME", "FargoVPN")
ensure("SUBSCRIPTION_DAYS", 30)
ensure("BOT_WELCOME_TEXT", "")
ensure("BOT_SUPPORT_PROMPT", "")
ensure("FAQ_INCY_URL", "https://apps.apple.com/ru/app/incy/id6756943388")
ensure("BOT_IDENTITY_REFRESH_SECONDS", 600)
ensure("BOT_SYNC_INTERVAL_SECONDS", 3600)
ensure("BACKUP_TELEGRAM", True)
ensure("BACKUP_TELEGRAM_PART_MB", 45)
ensure("BACKUP_KEEP_DAYS", 14)
ensure("BACKUP_INTERVAL_DAYS", 3)
ensure("BACKUP_RETRY_INTERVAL_SECONDS", 900)
ensure("BACKUP_PENDING_KEEP_DAYS", 30)
ensure("BACKUP_INCLUDE_VENV", True)
ensure("BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock")
ensure("BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json")
ensure("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
ensure("XUI_INTERNAL_BASE_URL", "")
ensure("XUI_INTERNAL_AUTO_DETECT", True)
ensure("XUI_CACHE_SECONDS", 30)
ensure("XUI_VERIFY_TLS", False)
def remove_setting(name):
    global text
    text = re.sub(rf"(?m)^{re.escape(name)}\s*=.*$\n?", "", text)
for legacy_web_setting in ("WEB_PORT", "WEB_MANAGE_UFW", "WEB_ALLOW_PLAIN_HTTP", "WEB_TLS_CERT_FILE", "WEB_TLS_KEY_FILE", "LETSENCRYPT_PANEL_ENABLED", "LETSENCRYPT_PANEL_EMAIL", "LETSENCRYPT_PANEL_CERTBOT_CONFIG_DIR"):
    remove_setting(legacy_web_setting)

for legacy_name in ("HTTPS_ENABLED", "HTTPS_DOMAIN", "LETSENCRYPT_EMAIL"):
    remove_setting(legacy_name)
ensure("REMINDER_DAYS", [7, 3, 1, 0])
ensure("REMINDER_LOCK_PATH", "/run/vpn-service-reminders.lock")
ensure("METRICS_STORE_INTERVAL_SECONDS", 60)
ensure("IDENTITY_IMPORT_MAX_MB", 512)
ensure("USER_EVENT_KEEP_DAYS", 365)
ensure("USER_EVENT_MAX_ROWS", 250000)
ensure("CHAT_MEDIA_MAX_MB", 100)
ensure("CHAT_MEDIA_CACHE_DIR", "/var/cache/vpn-service/chat-media")
ensure("CHAT_MEDIA_CACHE_DAYS", 30)
ensure("CHAT_MEDIA_CACHE_MAX_MB", 512)
ensure("BROADCAST_DIR", "/var/lib/vpn-service/broadcasts")
ensure("BROADCAST_MEDIA_MAX_MB", 45)
ensure("BROADCAST_SEND_DELAY_SECONDS", 0.04)
ensure("BROADCAST_STALE_SECONDS", 7200)
ensure("WEB_HOST", "127.0.0.1")
ensure("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")
ensure("APP_LOG_PATH", "/var/log/vpn_bot.log")
nginx_masked = Path("/etc/nginx/conf.d/01-main.conf").is_file() and Path("/etc/nginx/stream.d/00-stream.conf").is_file()
set_value("WEB_REVERSE_PROXY", True)
set_value("WEB_HOST", "127.0.0.1")
set_value("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")
set_value("WEB_COOKIE_HTTPS_ONLY", True)
set_value("WEB_TRUST_PROXY_HEADERS", True)
prefix = str(literal("WEB_PUBLIC_PREFIX", "") or "").strip()
if not re.fullmatch(r"/[A-Za-z0-9_-]{8,96}", prefix):
    prefix = "/fargovpn-admin-" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18].lower()
set_value("WEB_PUBLIC_PREFIX", prefix)
ensure("WEB_DOMAIN", "")
ensure("WEB_TLS_SERVER_NAME", "")
ensure("WEB_TIMEZONE", "Asia/Almaty")
# Prefer the existing primary-site Let's Encrypt certificate on this host.
# Keep the selection entirely inside Python so Bash syntax cannot leak into
# the heredoc and break the installer.
if not literal("WEB_DOMAIN", ""):
    discovered = ""
    for cfg_path in sorted(Path("/etc/nginx").glob("conf.d/*.conf")):
        try:
            nginx_text = cfg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"(?m)^\s*server_name\s+([^;]+);", nginx_text):
            for candidate in re.findall(r"[A-Za-z0-9_.-]+", match.group(1)):
                if candidate.lower() not in {"_", "localhost", "default"} and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,253}", candidate):
                    cert = Path("/etc/letsencrypt/live") / candidate / "fullchain.pem"
                    key = Path("/etc/letsencrypt/live") / candidate / "privkey.pem"
                    if cert.is_file() and key.is_file():
                        discovered = candidate
                        break
            if discovered:
                break
        if discovered:
            break
    if discovered:
        set_value("WEB_DOMAIN", discovered)
        set_value("WEB_TLS_SERVER_NAME", discovered)
ensure("WEB_USERNAME", "admin")
ensure("WEB_PASSWORD_HASH", "")
set_value("WEB_COOKIE_HTTPS_ONLY", True)
ensure("WEB_SESSION_MAX_AGE_SECONDS", 28800)
ensure("BOT_PANEL_URL", "")
domain_value = str(literal("WEB_DOMAIN", "") or literal("WEB_TLS_SERVER_NAME", "") or "").strip()
if domain_value and prefix:
    public_url = "https://" + domain_value + prefix.rstrip("/") + "/"
    set_value("BOT_PANEL_URL", public_url)
    set_value("PUBLIC_PANEL_URL", public_url)
ensure("CABINET_ENABLED", True)
ensure("CABINET_PATH", "/cabinet")
ensure("CABINET_SESSION_MAX_AGE_SECONDS", 86400)
set_value("WEB_REVERSE_PROXY", True)
set_value("WEB_HOST", "127.0.0.1")
set_value("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")
set_value("WEB_TRUST_PROXY_HEADERS", True)
if True:
    domain_value = str(literal("WEB_DOMAIN", "") or literal("WEB_TLS_SERVER_NAME", "") or "").strip()
    prefix_value = str(literal("WEB_PUBLIC_PREFIX", "") or "").strip()
    if domain_value and prefix_value and not str(literal("BOT_PANEL_URL", "") or "").strip():
        set_value("BOT_PANEL_URL", "https://" + domain_value + prefix_value.rstrip("/") + "/")
        set_value("PUBLIC_PANEL_URL", "https://" + domain_value + prefix_value.rstrip("/") + "/")
ensure("WEB_LOGIN_MAX_ATTEMPTS", 5)
ensure("WEB_LOGIN_WINDOW_SECONDS", 900)
ensure("WEB_LOGIN_BLOCK_SECONDS", 900)
ensure("WEB_LOGIN_MAX_BLOCK_SECONDS", 86400)
ensure("WEB_LOGIN_SECURITY_RETENTION_DAYS", 30)
legacy_password = str(literal("WEB_PASSWORD_HASH", "") or "")
if legacy_password and not legacy_password.startswith("pbkdf2_sha256$"):
    salt = secrets.token_bytes(16)
    iterations = 260000
    digest = hashlib.pbkdf2_hmac("sha256", legacy_password.encode("utf-8"), salt, iterations)
    set_value("WEB_PASSWORD_HASH", f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")
if not str(literal("WEB_SECRET_KEY", "") or "").strip():
    set_value("WEB_SECRET_KEY", secrets.token_hex(32))
ensure("RECEIPT_OCR_ENABLED", True)
ensure("RECEIPT_AUTO_APPROVE", True)
ensure("RECEIPT_MIN_AMOUNT", 150.0)
ensure("RECEIPT_MAX_AGE_HOURS", 24)
ensure("RECEIPT_RECEIVER_ALIASES", "")
ensure("RECEIPT_ALLOW_MASKED_PHONE", True)
ensure("RECEIPT_TIMEZONE", "Asia/Almaty")
ensure("RECEIPT_OCR_LANGUAGES", "rus+eng")
ensure("RECEIPT_OCR_TIMEOUT", 20)
ensure("RECEIPT_FILTER_NAME", True)
ensure("RECEIPT_FILTER_PHONE", False)
ensure("RECEIPT_FILTER_AMOUNT", True)
ensure("RECEIPT_FILTER_DATE", False)
ensure("RECEIPT_FILTER_STATUS", True)
ensure("RECEIPT_FILTER_DUPLICATE", True)
ensure("YANDEX_DISK_ENABLED", False)
ensure("YANDEX_DISK_MODE", "oauth")
ensure("YANDEX_DISK_TOKEN", "")
ensure("YANDEX_DISK_PATH", "VPN-Service-Backups")
ensure("YANDEX_LOCAL_PATH", "/mnt/yandex-disk")
ensure("YANDEX_LOCAL_REQUIRE_MOUNT", True)
ensure("YANDEX_WEBDAV_URL", "https://webdav.yandex.ru")
ensure("YANDEX_DAVFS_SECRETS_PATH", "/etc/davfs2/secrets")
ensure("YANDEX_UPLOAD_RETRIES", 3)
ensure("YANDEX_UPLOAD_VERIFY_ATTEMPTS", 8)
ensure("YANDEX_UPLOAD_VERIFY_DELAY", 1.5)
ensure("RCLONE_REMOTE", "")
ensure("RCLONE_PATH", "VPN-Service-Backups")
ensure("UPDATE_DIR", "/var/lib/vpn-service/updates")
ensure("UPDATE_PUBLISHER_USERNAME", str(literal("WEB_USERNAME", "admin")))
ensure("GITHUB_API_BASE_URL", "https://api.github.com")
ensure("GITHUB_API_TOKEN", "")
ensure("GITHUB_REPOSITORY_OWNER", "")
ensure("GITHUB_REPOSITORY_NAME", "FargoVPN")
ensure("GITHUB_TARGET_BRANCH", "main")
ensure("GITHUB_RELEASE_TAG_PREFIX", "FargoVPN-")
ensure("GITHUB_RELEASE_NAME_TEMPLATE", "FargoVPN {version}")
ensure("GITHUB_RELEASE_ASSET_NAME", "VPN_Service_Platform_{version}_FULL.tar.gz")
ensure("GITHUB_RELEASE_MAKE_LATEST", True)
ensure("GITHUB_RELEASE_DRAFT", False)
ensure("GITHUB_RELEASE_PRERELEASE", False)
ensure("UPDATE_CHECK_INTERVAL", 60)
ensure("UPDATE_VERIFY_TLS", True)
ensure("UPDATE_MAX_ARCHIVE_MB", 1024)
ensure("PUSH_ENABLED", True)
ensure("PUSH_VAPID_SUBJECT", "")
ensure("PUSH_VAPID_PRIVATE_KEY_PATH", "/var/lib/vpn-service/vapid_private.pem")
ensure("PUSH_VAPID_PUBLIC_KEY", "")
ensure("PUSH_TTL_SECONDS", 3600)
ensure("PUSH_MAX_SUBSCRIPTIONS_PER_USER", 8)
ensure("PUSH_TEST_ENABLED", True)
ensure("UPDATE_STALE_JOB_SECONDS", 7200)
if literal("UPDATE_IS_PUBLISHER", None) is None:
    set_value(
        "UPDATE_IS_PUBLISHER",
        os.environ["PROFILE"] == "full",
    )
path.write_text(text, encoding="utf-8")
# Never continue with a corrupted config.py.  This catches accidental writes of
# Nginx/server configuration before any later `import config` can mask the cause.
compile_source = path.read_text(encoding="utf-8")
try:
    compile(compile_source, str(path), "exec")
except SyntaxError as exc:
    first_lines = "\n".join(compile_source.splitlines()[:12])
    raise SystemExit(f"Сгенерированный config.py не является корректным Python ({exc}).\nПервые строки файла:\n{first_lines}")
PY

if [[ $PROFILE == full ]]; then
  mapfile -t WEB_INFO < <(target_python <<'PY'
import config
stored = str(getattr(config, "WEB_PASSWORD_HASH", ""))
parts = stored.split("$")
ready = stored.startswith("pbkdf2_sha256$") and len(parts) == 4 and bool(getattr(config, "WEB_USERNAME", ""))
print("1" if ready else "0")
print(str(getattr(config, "WEB_USERNAME", "admin") or "admin"))
PY
  )
  if [[ ${WEB_INFO[0]:-0} != 1 ]]; then
    [[ $NONINTERACTIVE -eq 0 ]] || { echo 'Для полного профиля не заданы WEB_USERNAME и WEB_PASSWORD_HASH; один раз запустите установщик в интерактивном режиме.' >&2; exit 1; }
    ask WEB_USER 'Логин веб-панели' "${WEB_INFO[1]:-admin}"
    ask WEB_PASS 'Пароль веб-панели' '' yes
    HASH=$("$TARGET/.venv/bin/python" - "$WEB_PASS" <<'PY'
import hashlib, secrets, sys
salt=secrets.token_bytes(16); iterations=260000
value=hashlib.pbkdf2_hmac("sha256", sys.argv[1].encode(), salt, iterations).hex()
print(f"pbkdf2_sha256${iterations}${salt.hex()}${value}")
PY
    )
    SECRET=$(openssl rand -hex 32)
    export TARGET WEB_USER HASH SECRET
    "$TARGET/.venv/bin/python" <<'PY'
from pathlib import Path
import os,re
path=Path(os.environ["TARGET"])/"config.py"; text=path.read_text(encoding="utf-8")
for name,value in {"WEB_USERNAME":os.environ["WEB_USER"],"WEB_PASSWORD_HASH":os.environ["HASH"],"WEB_SECRET_KEY":os.environ["SECRET"]}.items():
    line=f"{name} = {value!r}"
    pat=rf"(?m)^{re.escape(name)}\s*=.*$"
    text=re.sub(pat,line,text) if re.search(pat,text) else text.rstrip()+"\n"+line+"\n"
path.write_text(text,encoding="utf-8")
PY
  fi
fi

cat > "$TARGET/run_bot.sh" <<'EOF_BOT_RUN'
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if ! "$APP_DIR/.venv/bin/python" -c 'import config; raise SystemExit(0 if str(getattr(config, "BOT_TOKEN", "")).strip() else 1)'; then
  echo "Telegram-бот не запущен: BOT_TOKEN ещё не настроен. Укажите токен в веб-панели → Настройки → Бот и сервис." >&2
  exit 0
fi
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py"
EOF_BOT_RUN
chmod 755 "$TARGET/run_bot.sh"

if [[ $PROFILE == full ]]; then
  cat > "$TARGET/web_start.sh" <<'EOF_WEB_START'
#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/panel_runtime.py"
EOF_WEB_START
  chmod 755 "$TARGET/web_start.sh"
else
  rm -f "$TARGET/web_start.sh"
fi

if ! config_is_valid "$TARGET/config.py"; then
  echo "КРИТИЧЕСКАЯ ОШИБКА: итоговый config.py не является корректным Python." >&2
  sed -n '1,24p' "$TARGET/config.py" >&2 || true
  exit 1
fi
chmod 600 "$TARGET/config.py"

# Resolve the web socket path before any strict-mode heredoc expands it.
# External SOCKET_PATH remains supported; otherwise use the installed config value.
if [[ $PROFILE == full ]]; then
  SOCKET_PATH="${SOCKET_PATH:-$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")).strip())')}"
  SOCKET_PATH="${SOCKET_PATH:-/run/vpn-service/fargovpn.sock}"
  [[ "$SOCKET_PATH" = /* ]] || { echo "Некорректный SOCKET_PATH: $SOCKET_PATH (нужен абсолютный путь)." >&2; exit 1; }
fi
VPN_UPDATE_PROGRESS=86
write_update_status "migrating" "Проверяется конфигурация и обновляется база" "$VPN_UPDATE_PROGRESS" "database"
log_step "Проверка конфигурации и Python-модулей"
"$TARGET/.venv/bin/python" -m py_compile "$TARGET"/*.py "$TARGET"/services/*.py
target_python - <<'PY'
import importlib
import config
assert str(config.SERVICE_NAME).strip(), "Параметр SERVICE_NAME пуст"
modules = ["backup", "trigger_reminders", "sync_users", "service_audit", "time_utils"]
if str(getattr(config, "BOT_TOKEN", "")).strip():
    modules.insert(0, "main")
else:
    print("ℹ BOT_TOKEN пуст — импорт Telegram-бота пропущен; токен можно добавить позже в веб-панели.")
for module in modules:
    importlib.import_module(module)
if str(getattr(config, "INSTALL_PROFILE", "full")).lower() == "full":
    importlib.import_module("webapp")
print("Проверка импортов и синтаксиса Python: успешно")
PY
"$TARGET/.venv/bin/python" "$TARGET/init_db.py"
VPN_UPDATE_PROGRESS=89
write_update_status "migrating" "Конфигурация и структура базы данных проверены" "$VPN_UPDATE_PROGRESS" "database"

VPN_UPDATE_PROGRESS=91
write_update_status "installing" "Обновляются systemd-службы" "$VPN_UPDATE_PROGRESS" "services"
if ! cat > /etc/systemd/system/vpn-service-bot.service <<EOF_UNIT
[Unit]
Description=Telegram-бот VPN Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/run_bot.sh
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Almaty
UMask=0077
StandardOutput=append:/var/log/vpn_bot.log
StandardError=append:/var/log/vpn_bot.log

[Install]
WantedBy=multi-user.target
EOF_UNIT
then
  echo "Не удалось записать /etc/systemd/system/vpn-service-bot.service" >&2
  exit 1
fi

if [[ $PROFILE == full ]]; then
    install -d -m 0755 /run/vpn-service
  cat > /etc/tmpfiles.d/vpn-service.conf <<EOF_TMPFILES
d /run/vpn-service 0755 root root -
EOF_TMPFILES
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /etc/tmpfiles.d/vpn-service.conf
  fi

  NGINX_SOCKET_GROUP=$(detect_nginx_socket_group) || {
    echo "Не удалось определить группу Nginx для FargoVPN Unix socket." >&2
    exit 1
  }
  echo "Группа доступа Nginx для FargoVPN socket: $NGINX_SOCKET_GROUP"

  if ! cat > /etc/systemd/system/vpn-service-web.socket <<EOF_UNIT
[Unit]
Description=Unix socket for FargoVPN web panel

[Socket]
ListenStream=$SOCKET_PATH
SocketUser=root
SocketGroup=$NGINX_SOCKET_GROUP
SocketMode=0660
DirectoryMode=0755
RemoveOnStop=no

[Install]
WantedBy=sockets.target
EOF_UNIT
  then
    echo "Не удалось записать /etc/systemd/system/vpn-service-web.socket" >&2
    exit 1
  fi

  if ! cat > /etc/systemd/system/vpn-service-web.service <<EOF_UNIT
[Unit]
Description=Веб-панель VPN Service
Requires=vpn-service-web.socket nginx.service
After=network-online.target nginx.service vpn-service-web.socket
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/web_start.sh
StandardOutput=append:/var/log/vpn_bot.log
StandardError=append:/var/log/vpn_bot.log
UMask=0077
Restart=on-failure
RestartSec=5
# Не держать обновление/перезапуск из-за долгоживущих SSE-соединений.
TimeoutStopSec=6
KillMode=mixed
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Almaty

[Install]
WantedBy=multi-user.target
EOF_UNIT
  then
    echo "Не удалось записать /etc/systemd/system/vpn-service-web.service" >&2
    exit 1
  fi
  if [[ -f "$TARGET/vpn-service-nginx-guard.service" ]]; then
    sed "s#@TARGET@#$TARGET#g" "$TARGET/vpn-service-nginx-guard.service" > /etc/systemd/system/vpn-service-nginx-guard.service
    chmod 0644 /etc/systemd/system/vpn-service-nginx-guard.service
  fi
else
  systemctl disable --now vpn-service-web.service >/dev/null 2>&1 || true
  systemctl disable --now vpn-service-nginx-guard.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vpn-service-nginx-guard.service
  systemctl disable --now vpn-service-web.socket >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vpn-service-web.service /etc/systemd/system/vpn-service-web.socket
fi

if [[ $PROFILE == full ]]; then
  if ! cat > /etc/systemd/system/vpn-service-update@.service <<EOF_UNIT
[Unit]
Description=Фоновое обновление VPN Service (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/.venv/bin/python $TARGET/update_worker.py --job-id %i --startup-delay 4
Nice=10
IOSchedulingClass=best-effort
TimeoutStartSec=infinity
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Almaty
EOF_UNIT
  then
    echo "Не удалось записать /etc/systemd/system/vpn-service-update@.service" >&2
    exit 1
  fi

  if ! cat > /etc/systemd/system/vpn-service-broadcast@.service <<EOF_UNIT
[Unit]
Description=Массовая Telegram-рассылка VPN Service (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/.venv/bin/python $TARGET/broadcast_worker.py --job-id %i --startup-delay 1
Nice=10
IOSchedulingClass=best-effort
TimeoutStartSec=infinity
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Almaty
EOF_UNIT
  then
    echo "Не удалось записать /etc/systemd/system/vpn-service-broadcast@.service" >&2
    exit 1
  fi
else
  rm -f \
    /etc/systemd/system/vpn-service-update@.service \
    /etc/systemd/system/vpn-service-broadcast@.service
fi

if ! cat > /etc/systemd/system/vpn-service-backup.service <<EOF_UNIT
[Unit]
Description=Полная резервная копия VPN Service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/.venv/bin/python $TARGET/backup.py --scheduled
Environment=TZ=Asia/Almaty
EOF_UNIT
then
  echo "Не удалось записать /etc/systemd/system/vpn-service-backup.service" >&2
  exit 1
fi

if ! cat > /etc/systemd/system/vpn-service-backup.timer <<'EOF_UNIT'
[Unit]
Description=Периодическая проверка резервной копии и повторной доставки VPN Service

[Timer]
OnBootSec=5m
OnUnitActiveSec=15m
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF_UNIT
then
  echo "Не удалось записать /etc/systemd/system/vpn-service-backup.timer" >&2
  exit 1
fi

if ! cat > /etc/systemd/system/vpn-service-reminders.service <<EOF_UNIT
[Unit]
Description=Напоминания о VPN-подписке
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=$TARGET
ExecStart=$TARGET/.venv/bin/python $TARGET/trigger_reminders.py
EOF_UNIT
then
  echo "Не удалось записать /etc/systemd/system/vpn-service-reminders.service" >&2
  exit 1
fi

if ! cat > /etc/systemd/system/vpn-service-reminders.timer <<'EOF_UNIT'
[Unit]
Description=Ежедневные напоминания о VPN-подписке

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
AccuracySec=5m

[Install]
WantedBy=timers.target
EOF_UNIT
then
  echo "Не удалось записать /etc/systemd/system/vpn-service-reminders.timer" >&2
  exit 1
fi

systemctl daemon-reload
VPN_UPDATE_PROGRESS=94
write_update_status "installing" "Файлы systemd-служб обновлены" "$VPN_UPDATE_PROGRESS" "services"
"$TARGET/.venv/bin/python" "$TARGET/service_audit.py" --fix --json >/var/log/vpn-service-audit-install.json 2>&1 || true
# Предыдущая установка могла быть запущена вручную, вне systemd. Такой процесс
# удерживает Telegram getUpdates и создаёт впечатление, что новый бот не работает.
# Жизненным циклом FargoVPN управляет только systemd. Ручное убийство
# panel_runtime.py здесь запрещено: при Restart=on-failure это создавало
# гонку и серию неожиданных stop/start во время установки/обновления.
systemctl stop vpn-service-web.service vpn-service-web.socket fargovpn-web.service fargovpn-web.socket 2>/dev/null || true
systemctl daemon-reload
VERIFY_UNITS=(
  /etc/systemd/system/vpn-service-bot.service
  /etc/systemd/system/vpn-service-backup.service
  /etc/systemd/system/vpn-service-backup.timer
  /etc/systemd/system/vpn-service-reminders.service
  /etc/systemd/system/vpn-service-reminders.timer
)
if [[ $PROFILE == full ]]; then
  VERIFY_UNITS+=(
    /etc/systemd/system/vpn-service-web.service
    /etc/systemd/system/vpn-service-web.socket
    /etc/systemd/system/vpn-service-update@.service
    /etc/systemd/system/vpn-service-broadcast@.service
    /etc/systemd/system/vpn-service-nginx-guard.service
  )
fi
if [[ $PROFILE == full ]]; then
  [[ ${MASK_READY:-0} -eq 1 ]] || {
    echo 'Критическая ошибка: Nginx L4 Stream Router Mask не подтверждён перед запуском FargoVPN.' >&2
    exit 1
  }
  systemctl is-active --quiet nginx.service || {
    echo 'Критическая ошибка: nginx.service не active перед запуском FargoVPN.' >&2
    exit 1
  }
fi

if ! systemd-analyze verify "${VERIFY_UNITS[@]}"; then
  echo 'Проверка unit-файлов systemd завершилась ошибкой. Службы не запущены.' >&2
  exit 1
fi

VPN_UPDATE_PROGRESS=96
write_update_status "restarting" "Перезапускаются службы" "$VPN_UPDATE_PROGRESS" "restart"
systemctl enable vpn-service-bot.service vpn-service-backup.timer vpn-service-reminders.timer
if [[ $PROFILE == full ]]; then
  systemctl enable vpn-service-web.socket
  systemctl enable vpn-service-web.service
  # Чистая socket-activation транзакция: старый listener закрывается полностью,
  # затем создаётся новый и только после этого запускается web service.
  systemctl stop vpn-service-web.service vpn-service-web.socket >/dev/null 2>&1 || true
  rm -f "$SOCKET_PATH".stale "$SOCKET_PATH" 2>/dev/null || true
  systemctl start vpn-service-web.socket
  systemctl is-active --quiet vpn-service-web.socket || {
    echo 'Критическая ошибка: vpn-service-web.socket не запустился.' >&2
    journalctl -u vpn-service-web.socket -n 80 --no-pager || true
    exit 1
  }
  systemctl start vpn-service-web.service
  if ! systemctl is-active --quiet vpn-service-web.service; then
    echo 'Критическая ошибка: vpn-service-web.service не перешёл в active после запуска.' >&2
    systemctl status vpn-service-web.service --no-pager -n 80 >&2 || true
    exit 1
  fi
  if [[ -f /etc/systemd/system/vpn-service-nginx-guard.service ]]; then
    systemctl enable vpn-service-nginx-guard.service
  fi
fi
systemctl restart vpn-service-bot.service
if [[ $PROFILE == full ]]; then
  # Не выполняем второй restart web.service: сервис уже запущен в чистой
  # socket-activation транзакции выше. Второй restart создавал наблюдаемую гонку.
  if [[ -f /etc/systemd/system/vpn-service-nginx-guard.service ]]; then
    if ! "$TARGET/.venv/bin/python" "$TARGET/nginx_panel_guard.py" --once; then
      echo "Не удалось применить FargoVPN location в Nginx. Установка остановлена для сохранения рабочей панели." >&2
      nginx -t 2>&1 || true
      exit 1
    fi
    # The application owns the public prefix. No FargoVPN location may retain
    # proxy_redirect/proxy_cookie_path, which would turn /prefix/ into /prefix/prefix/.
    PANEL_PREFIX_CHECK=$(target_python -c 'import config; print(str(getattr(config,"WEB_PUBLIC_PREFIX","") or "").strip().rstrip("/"))')
    if python3 - "$PANEL_PREFIX_CHECK" <<'PY_NGINX_CHECK'
import subprocess, sys
prefix = (sys.argv[1] or '').rstrip('/')
text = subprocess.run(['nginx', '-T'], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False).stdout
needle = f'location ^~ {prefix}/' if prefix else ''
start = text.find(needle)
if start < 0:
    raise SystemExit(1)
block = text[start:]
# Limit the scan to the matching location block using brace depth.
depth = 0
lines = []
started = False
for line in block.splitlines():
    opens = line.count('{')
    closes = line.count('}')
    if not started:
        started = True
    lines.append(line)
    depth += opens - closes
    if started and depth <= 0:
        break
body = '\n'.join(lines)
raise SystemExit(1 if ('proxy_redirect' in body or 'proxy_cookie_path' in body) else 0)
PY_NGINX_CHECK
    then
      :
    else
      echo "Обнаружены запрещённые FargoVPN proxy_redirect/proxy_cookie_path в Nginx." >&2
      nginx -T 2>&1 | grep -nE 'fargovpn-admin|proxy_redirect|proxy_cookie_path' >&2 || true
      exit 1
    fi
    systemctl restart vpn-service-nginx-guard.service
    if ! systemctl is-active --quiet vpn-service-nginx-guard.service; then
      echo "Служба vpn-service-nginx-guard не запустилась." >&2
      journalctl -u vpn-service-nginx-guard -n 100 --no-pager || true
      exit 1
    fi
  fi
fi
systemctl restart vpn-service-backup.timer
systemctl restart vpn-service-reminders.timer

VPN_UPDATE_PROGRESS=98
write_update_status "health-check" "Проверяется запуск бота и веб-панели" "$VPN_UPDATE_PROGRESS" "health"
BOT_READY=$(target_python -c 'import config; print("1" if str(getattr(config, "BOT_TOKEN", "")).strip() else "0")')
if [[ "$BOT_READY" == "1" ]]; then
  systemctl is-active --quiet vpn-service-bot.service || {
    journalctl -u vpn-service-bot -n 120 --no-pager
    exit 1
  }
else
  echo "ℹ Telegram-бот пока не запускается: BOT_TOKEN ещё не задан. Его можно добавить в Настройки → Бот и сервис."
fi

if [[ $PROFILE == full ]]; then
  "$TARGET/.venv/bin/python" "$TARGET/panel_runtime.py" --check >/dev/null
  SOCKET_PATH=$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")))')
  HEALTH_OK=0
  HEALTH_BODY=''
  for _ in $(seq 1 60); do
    if [[ -S "$SOCKET_PATH" ]]; then
      HEALTH_BODY=$(curl --unix-socket "$SOCKET_PATH" -fsS --connect-timeout 2 --max-time 5 -H 'Cache-Control: no-cache' http://localhost/health 2>/dev/null || true)
      if [[ "$HEALTH_BODY" == OK* ]]; then
        HEALTH_OK=1
        break
      fi
    fi
    sleep 1
  done
  if [[ $HEALTH_OK -ne 1 ]]; then
    echo "Веб-панель не отвечает через Unix socket: $SOCKET_PATH." >&2
    echo "Состояние vpn-service-web.socket:" >&2
    systemctl is-active vpn-service-web.socket >&2 || true
    systemctl status vpn-service-web.socket --no-pager -n 40 >&2 || true
    echo "Состояние vpn-service-web.service:" >&2
    systemctl is-active vpn-service-web.service >&2 || true
    systemctl status vpn-service-web.service --no-pager -n 80 >&2 || true
    echo "Сведения о Unix socket:" >&2
    ls -l "$SOCKET_PATH" >&2 2>/dev/null || true
    stat -c 'path=%n type=%F mode=%a uid=%u gid=%g size=%s' "$SOCKET_PATH" >&2 2>/dev/null || true
    ss -xlpn 2>/dev/null | grep -F "$SOCKET_PATH" >&2 || true
    echo "Проверка VERSION на диске:" >&2
    cat "$TARGET/VERSION" >&2 || true
    echo "Последние строки журнала веб-панели:" >&2
    journalctl -u vpn-service-web -n 150 --no-pager >&2 || true
    exit 1
  fi
  [[ -f "$TARGET/VERSION" ]] || { echo "VERSION на диске не найден после запуска веб-панели." >&2; exit 1; }
  INSTALLED_VERSION=$(tr -d '[:space:]' < "$TARGET/VERSION")
  [[ "$INSTALLED_VERSION" == "$VERSION" ]] || { echo "Веб-панель запустилась, но версия на диске ($INSTALLED_VERSION) не совпадает с ожидаемой ($VERSION)." >&2; exit 1; }

  SW_BODY=$(curl --unix-socket "$SOCKET_PATH" -fsS --connect-timeout 2 --max-time 5 http://localhost/service-worker.js 2>/dev/null || true)
  if [[ -z "$SW_BODY" ]] || ! grep -Fq "const VERSION = '$VERSION';" <<<"$SW_BODY"; then
    echo "Service Worker не содержит актуальную версию $VERSION после установки." >&2
    exit 1
  fi

  # FargoVPN depends on the live 3x-ui API for users, traffic, online state and
  # server status. Verify the same integration layer used by the web panel.
  XUI_SMOKE=$(target_python - <<'PY_XUI_SMOKE'
from services.xui_api import request_json_sync
import json
results = {}
for name, method, path in (
    ("clients", "GET", "panel/api/clients/list"),
    ("inbounds", "GET", "panel/api/inbounds/list"),
    ("server", "GET", "panel/api/server/status"),
):
    data = request_json_sync(method, path)
    results[name] = isinstance(data, (dict, list))
if not all(results.values()):
    raise SystemExit(json.dumps(results, ensure_ascii=False))
print("3x-ui API: clients/list, inbounds/list, server/status — OK")
PY_XUI_SMOKE
  ) || {
    echo 'Критическая ошибка: FargoVPN не получил корректный ответ от 3x-ui API.' >&2
    echo "$XUI_SMOKE" >&2
    exit 1
  }
  echo "$XUI_SMOKE"

  # Mandatory transport validation: the installed release must be reachable
  # through the existing HTTPS 443 router, not merely on its local socket.
  target_python - <<'PY' > "$TARGET/.fargovpn-public-url"
import config
from urllib.parse import urlsplit
value = str(getattr(config, "WEB_DOMAIN", "") or "").strip()
prefix = str(getattr(config, "WEB_PUBLIC_PREFIX", "") or "").strip().rstrip("/")
if not value or not prefix:
    raise SystemExit(2)
if "://" not in value:
    value = "https://" + value
host = urlsplit(value).hostname or ""
if not host:
    raise SystemExit(3)
print(f"https://{host}{prefix}")
PY
  PUBLIC_BASE=$(cat "$TARGET/.fargovpn-public-url")
  rm -f "$TARGET/.fargovpn-public-url"
  if grep -RqsE 'proxy_pass[[:space:]]+https?://127\.0\.0\.1:8088|proxy_pass[[:space:]]+http://localhost:8088' /etc/nginx/conf.d /etc/nginx/stream.d 2>/dev/null; then
    echo "Обнаружен legacy FargoVPN upstream :8088 в конфигурации Nginx." >&2
    nginx -T 2>&1 | grep -nE '8088|FARGOVPN' | head -n 120 >&2 || true
    exit 1
  fi
  if ss -lnt 2>/dev/null | grep -Eq '(^|[[:space:]])([0-9.:]+|\[[^]]+\]):8088([[:space:]]|$)'; then
    echo "Обнаружен TCP listener на 8088; новая версия требует полного удаления этого транспорта." >&2
    exit 1
  fi
  PUBLIC_HOST=$(target_python -c 'import config; from urllib.parse import urlsplit; v=str(getattr(config,"WEB_DOMAIN","") or "").strip(); v=("https://"+v) if "://" not in v else v; print(urlsplit(v).hostname or "")')
  PUBLIC_PATH=$(target_python -c 'import config; print(str(getattr(config,"WEB_PUBLIC_PREFIX","") or "").strip().rstrip("/"))')
  # Final Nginx verification is intentionally self-healing: the same guard
  # implementation is invoked again if a generated/included Nginx file was
  # reloaded between installation phases. We never accept a missing route.
  ROUTE_OK=0
  for _ in 1 2 3; do
    if verify_fargovpn_nginx_route "$PUBLIC_PATH" "$SOCKET_PATH"; then
      ROUTE_OK=1
      break
    fi
    echo "FargoVPN location пока не обнаружен; повторно синхронизирую конфигурацию Nginx..." >&2
    if [[ -x "$TARGET/.venv/bin/python" && -f "$TARGET/nginx_panel_guard.py" ]]; then
      "$TARGET/.venv/bin/python" "$TARGET/nginx_panel_guard.py" --once || true
    fi
    sleep 1
  done
  if [[ $ROUTE_OK -ne 1 ]]; then
    echo "Критическая ошибка: FargoVPN location ${PUBLIC_PATH}/ отсутствует в итоговой конфигурации Nginx." >&2
    nginx -T 2>&1 | grep -nE 'configuration file|fargovpn-admin|server_name|listen .*443|listen .*9443' | head -n 220 >&2 || true
    exit 1
  fi
  PUBLIC_HEALTH_OK=0
  for _ in $(seq 1 15); do
    if curl -4 -kfsS --connect-timeout 2 --max-time 5 --resolve "${PUBLIC_HOST}:443:127.0.0.1" "https://${PUBLIC_HOST}${PUBLIC_PATH}/health" 2>/dev/null | grep -Fq "$VERSION"; then
      PUBLIC_HEALTH_OK=1
      break
    fi
    sleep 1
  done
  if [[ $PUBLIC_HEALTH_OK -ne 1 ]]; then
    echo "Публичная проверка FargoVPN через HTTPS 443 не прошла: $PUBLIC_BASE/health" >&2
    echo "Nginx worker group: $(detect_nginx_socket_group 2>/dev/null || echo unknown)" >&2
    stat -c 'FargoVPN socket: %n type=%F mode=%a owner=%U group=%G' "$SOCKET_PATH" 2>/dev/null || true
    nginx -T 2>&1 | grep -nE 'FARGOVPN|proxy_pass|server_name|listen .*443' | head -n 180 >&2 || true
    exit 1
  fi
  # Also print the Mask credentials at the final successful stage. This covers
  # update/reinstall runs where the Mask itself was already installed and its
  # installer was therefore not executed in this run.
  if [[ -f /root/vpn_credentials.txt && ${MASK_ACCESS_PRINTED:-0} -eq 0 ]]; then
    print_mask_access_info
  fi
fi

systemctl reset-failed >/dev/null 2>&1 || true
if [[ $PROFILE == full ]]; then
  PROFILE_LABEL="полный (full)"
  PROFILE_STATUS="Полный профиль"
else
  PROFILE_LABEL="облегчённый (lite)"
  PROFILE_STATUS="Облегчённый профиль"
fi
write_update_status "completed" "$PROFILE_STATUS установлен; mask=${MASK_MODE}; проверки служб пройдены" "100" "complete"
IP=$(hostname -I | awk '{print $1}')
echo '========================================'
echo "Установка/обновление завершено: версия $VERSION"
echo "Профиль: $PROFILE_LABEL"
echo "Каталог приложения: $TARGET"
if [[ $PROFILE == full ]]; then
  PANEL_SCHEME="https"
  PANEL_HOST=$(target_python - <<'PY'
import config
from urllib.parse import urlsplit
value = str(getattr(config, "WEB_DOMAIN", "") or getattr(config, "BOT_PANEL_URL", "") or "").strip()
if "://" in value:
    host = urlsplit(value).hostname or ""
else:
    host = value.split(":", 1)[0].strip()
print(host or "SERVER_IP")
PY
  )
  PANEL_PREFIX=$(target_python -c 'import config; print(str(getattr(config, "WEB_PUBLIC_PREFIX", "")).rstrip("/"))')
  SOCKET_PATH=$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")))')
  echo "Веб-панель: ${PANEL_SCHEME}://${PANEL_HOST}${PANEL_PREFIX}/"
  echo "Backend: Unix socket $SOCKET_PATH"
  echo "Публичный вход FargoVPN: HTTPS 443. Отдельный TCP-порт панели не используется."
  echo "3x-ui остаётся на своём уникальном защищённом пути/маршруте."
fi
echo "Настройка локального WebDAV Яндекс.Диска: $TARGET/configure_yandex_webdav.sh"

