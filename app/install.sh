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
VERSION="$(cat "$SRC/VERSION" 2>/dev/null || true)"
if [[ -z "$VERSION" || ! "$VERSION" =~ ^[0-9]+\.[0-9]+([.][0-9]+)?([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "Критическая ошибка: в исходном дереве отсутствует корректный VERSION." >&2
  exit 1
fi
TMP=""
PREUPDATE_BACKUP=""
PREUPDATE_BACKUP_PART=""
PREUPDATE_SYSTEMD_DIR=""
NONINTERACTIVE=0
MODE=""
OLD=""
RESTORE=""
RESTORE_DIR=""
RESTORE_MODE=""
RESTORE_PREP=""
RESTORE_ROOT=""
PROFILE="full"
UPDATE_PATH=""

usage() {
  cat <<USAGE
Использование: $0 [--profile <старый-профиль>] [--update-existing /путь/к/приложению]

Параметр --profile оставлен только для совместимости со старыми установками; в 5.1.4 всегда используется полный профиль.
Без параметра --update-existing установщик предлагает:
  1) новую установку
  2) обновление существующей установки
  3) восстановление из папки с частями резервной копии (пользователи или вся система)

USAGE
}

while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo 'После --profile необходимо указать прежнее значение профиля.' >&2; exit 2; }
      # Совместимость с 4.x: старый updater мог передавать --profile <значение>.
      # В 5.1.4 профиль не выбирается — всегда используется полный вариант.
      LEGACY_PROFILE="$2"
      [[ -n "$LEGACY_PROFILE" ]] || { echo 'Значение --profile не может быть пустым.' >&2; exit 2; }
      echo "ℹ Получен устаревший параметр --profile; в 5.1.4 используется полный профиль."
      PROFILE=full
      shift 2
      ;;
    --profile=*)
      LEGACY_PROFILE="${1#*=}"
      [[ -n "$LEGACY_PROFILE" ]] || { echo 'Значение --profile не может быть пустым.' >&2; exit 2; }
      echo "ℹ Получен устаревший параметр --profile; в 5.1.4 используется полный профиль."
      PROFILE=full
      shift
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

if [[ -n "$UPDATE_PATH" ]]; then
  TARGET="$UPDATE_PATH"
fi

# A bootstrap invoked as curl | bash inherits the script pipe as stdin.
# All interactive reads must use the controlling terminal instead. Explicit
# panel updates keep stdin untouched and never prompt.
if [[ -z "$UPDATE_PATH" && ! -t 0 ]]; then
  if ! exec 0</dev/tty; then
    echo 'Нет доступного терминала для меню. Для автоматического обновления используйте --update-existing /root/vpn_bot.' >&2
    exit 2
  fi
fi

INSTALL_LOG="/var/log/vpn-service-install.log"
mkdir -p "$(dirname "$INSTALL_LOG")"
# Сохраняем полный журнал установки, не скрывая вывод в терминале.
exec > >(tee -a "$INSTALL_LOG") 2>&1

log_step() {
  echo
  echo "[Установщик VPN Service Platform] $*"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Не найдена обязательная команда: $1" >&2
    return 1
  fi
}

abort_install() {
  echo "Критическая ошибка: $*" >&2
  return 1
}


check_disk_space() {
  local path="$1" label="$2" min_kb="$3" mode="${4:-hard}" free_kb mount_point free_mb min_mb
  path="${path:-/}"
  if [[ ! -e "$path" ]]; then
    # Проверяем именно будущий каталог: df по несуществующему пути может вернуть ошибку,
    # хотя его родительский раздел полностью доступен для установки.
    mkdir -p -- "$path" 2>/dev/null || true
  fi
  free_kb=$(df -Pk -- "$path" 2>/dev/null | awk 'NR==2 {print $4}')
  mount_point=$(df -Pk -- "$path" 2>/dev/null | awk 'NR==2 {print $6}')
  if [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then
    if [[ "$mode" == soft ]]; then
      echo "ℹ Не удалось определить свободное место для $label ($path); продолжаю с мягким предупреждением."
      return 0
    fi
    echo "Не удалось определить свободное место для $label: $path." >&2
    return 1
  fi
  if (( free_kb < min_kb )); then
    free_mb=$((free_kb / 1024)); min_mb=$((min_kb / 1024))
    if [[ "$mode" == soft ]]; then
      echo "⚠ Мягкое предупреждение: в $label ($path, раздел ${mount_point:-неизвестен}) доступно около ${free_mb} МБ; желательно не менее ${min_mb} МБ для временных файлов." >&2
      return 0
    fi
    echo "Недостаточно места: $label ($path, раздел ${mount_point:-неизвестен}) содержит только около ${free_mb} МБ, а требуется не менее ${min_mb} МБ." >&2
    echo "Освободите место на этом разделе или повторите установку с целевым каталогом на разделе с достаточным свободным пространством." >&2
    return 1
  fi
  echo "✓ $label: свободно $((free_kb / 1024)) МБ на разделе ${mount_point:-неизвестен}"
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

  check_disk_space "$TARGET" "целевого каталога" 1048576
  check_disk_space "$BACKUP" "каталога резервных копий" 524288
  check_disk_space "$PACKAGE_ROOT" "временного каталога установщика" 256000 soft
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

pip_requirements_ok() {
  local requirements=$1
  "$TARGET/.venv/bin/python" - "$requirements" <<'PYPIPCHECK'
from importlib import metadata
from pathlib import Path
import sys

try:
    from packaging.requirements import Requirement
    from packaging.version import Version
except Exception as exc:
    print(f"packaging недоступен: {exc}", file=sys.stderr)
    raise SystemExit(2)

path = Path(sys.argv[1])
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or line.startswith(("-", "--")):
        continue
    requirement = Requirement(line)
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    try:
        installed = metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        print(f"Отсутствует пакет {requirement.name}", file=sys.stderr)
        raise SystemExit(1)
    if requirement.specifier and Version(installed) not in requirement.specifier:
        print(f"Пакет {requirement.name} версии {installed} не соответствует {requirement.specifier}", file=sys.stderr)
        raise SystemExit(1)
print("Все прямые зависимости requirements удовлетворены")
PYPIPCHECK
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
     && "$TARGET/.venv/bin/python" -c 'import pip' >/dev/null 2>&1 \
     && pip_requirements_ok "$requirements" >/dev/null 2>&1 \
     && "$TARGET/.venv/bin/python" -m pip check >/dev/null 2>&1; then
    log_step "Python-зависимости актуальны и проверены; существующее venv используется повторно"
    return 0
  fi

  log_step "Устанавливаются/проверяются Python-зависимости из $requirements"
  "$TARGET/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
  "$TARGET/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade -r "$requirements"
  pip_requirements_ok "$requirements"
  "$TARGET/.venv/bin/python" -m pip check
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

# Финальная сводка печатает только компактные состояния. Полный вывод команд
# остаётся в INSTALL_LOG и не смешивается с итоговым экраном установщика.
declare -A SUMMARY_STATUS=()
declare -A SUMMARY_DETAIL=()
CURRENT_SUMMARY_PHASE="preflight"

summary_running() {
  local phase=$1 detail=${2:-Выполняется}
  CURRENT_SUMMARY_PHASE=$phase
  SUMMARY_STATUS[$phase]="…"
  SUMMARY_DETAIL[$phase]=$detail
}

summary_ok() {
  local phase=$1 detail=${2:-готово}
  SUMMARY_STATUS[$phase]="✓"
  SUMMARY_DETAIL[$phase]=$detail
}

summary_fail() {
  local phase=$1 detail=${2:-ошибка}
  SUMMARY_STATUS[$phase]="✗"
  SUMMARY_DETAIL[$phase]=$detail
}

service_failure_details() {
  local unit=$1 log_file=${2:-/var/log/vpn_bot.log}
  echo "Служба $unit не готова. Подробности: $log_file и $INSTALL_LOG" >&2
}

summary_unit() {
  local unit=$1 label=$2
  local state sub result restarts
  state=$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || true)
  sub=$(systemctl show "$unit" -p SubState --value 2>/dev/null || true)
  result=$(systemctl show "$unit" -p Result --value 2>/dev/null || true)
  restarts=$(systemctl show "$unit" -p NRestarts --value 2>/dev/null || true)
  if [[ -n "$state" ]]; then
    printf '  %-24s %s/%s result=%s restarts=%s\n' "$label" "$state" "${sub:-?}" "${result:-?}" "${restarts:-0}"
  else
    printf '  %-24s не установлен\n' "$label"
  fi
}

summary_postgresql() {
  local dsn='' host='' port='' db=''
  if [[ -f "$TARGET/config.py" && -x "$TARGET/.venv/bin/python" ]]; then
    dsn=$(target_python -c 'import config; print(str(getattr(config, "DATABASE_URL", "") or ""))' 2>/dev/null || true)
    if [[ -z "$dsn" ]]; then
      dsn=$(target_python -c 'import ast, pathlib; t=pathlib.Path("config.py").read_text(encoding="utf-8");
for n in ast.parse(t).body:
    if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id=="DATABASE_URL" for x in n.targets):
        try: print(ast.literal_eval(n.value) or "")
        except Exception: pass
        break' 2>/dev/null || true)
    fi
  fi
  if [[ "$dsn" == postgresql* ]]; then
    host=$(printf '%s\n' "$dsn" | sed -nE 's#^[^:]+://[^@]+@([^:/]+)(:[0-9]+)?/.*#\1#p')
    port=$(printf '%s\n' "$dsn" | sed -nE 's#^[^:]+://[^@]+@[^:/]+:([0-9]+)/.*#\1#p')
    db=$(printf '%s\n' "$dsn" | sed -nE 's#.*/([^/?]+)(\?.*)?$#\1#p')
    echo "  PostgreSQL              ${host:-local}:${port:-5432}/${db:-?}"
  else
    echo '  PostgreSQL               DSN не настроен в config.py'
  fi
}

summary_xui_service() {
  local unit
  for unit in x-ui.service 3x-ui.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      summary_unit "$unit" '3x-ui'
      return 0
    fi
  done
  echo '  3x-ui                   unit не найден'
}

summary_nginx_service() {
  if systemctl cat nginx.service >/dev/null 2>&1; then
    summary_unit nginx.service 'Внешний Nginx'
  else
    echo '  Внешний Nginx            unit не найден'
  fi
}

summary_3xui() {
  local base=''
  if [[ -f "$TARGET/config.py" && -x "$TARGET/.venv/bin/python" ]]; then
    base=$(target_python -c 'import ast, pathlib; t=pathlib.Path("config.py").read_text(encoding="utf-8");
for n in ast.parse(t).body:
    if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id=="BASE_URL" for x in n.targets):
        print(ast.literal_eval(n.value) or ""); break' 2>/dev/null || true)
  fi
  [[ -n "$base" ]] && echo "  3x-ui                   $base" || echo '  3x-ui                   адрес не настроен'
}

print_update_summary() {
  local panel_url='' prefix='' expected="$VERSION"
  echo
  echo '========================================'
  echo ' FargoVPN — итог установки/обновления'
  echo '========================================'
  printf 'Версия:                  %s\n' "$expected"
  printf 'Каталог:                 %s\n' "$TARGET"
  echo
  echo 'Шаги:'
  local phase
  for phase in preflight dependencies stop-services backup files python database services nginx restart health; do
    printf '  %-18s %s %s\n' "$phase" "${SUMMARY_STATUS[$phase]:-–}" "${SUMMARY_DETAIL[$phase]:-не выполнялся}"
  done
  echo
  if [[ -x "$TARGET/.venv/bin/python" && -f "$TARGET/config.py" ]]; then
    if [[ "$PROFILE" == full ]]; then
      panel_url=$(target_python "$TARGET/nginx_panel_guard.py" --print-url 2>/dev/null || true)
      if [[ -n "$panel_url" ]]; then
        PANEL_PUBLIC_URL="$panel_url"
        echo "Веб-панель FargoVPN: $PANEL_PUBLIC_URL"
      else
        prefix=$(target_python -c 'import config; print(str(getattr(config, "WEB_PUBLIC_PREFIX", "")).rstrip("/"))' 2>/dev/null || true)
        [[ -n "$prefix" ]] && echo "Панель:                  ${prefix}/ через внешний Nginx :443"
        echo 'Внешний Nginx:            используется существующий, FargoVPN его не устанавливает'
      fi
      local socket=''
      socket=$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")))' 2>/dev/null || true)
      [[ -n "$socket" ]] && echo "Backend socket:           $socket"
      local web_user=''
      web_user=$(target_python -c 'import config; print(str(getattr(config, "WEB_USERNAME", "")))' 2>/dev/null || true)
      [[ -n "$web_user" ]] && echo "Логин панели:             $web_user"
    fi
  fi
  echo
  echo 'Порты/службы:'
  echo '  HTTPS                    443 / внешний Nginx'
  summary_unit vpn-service-bot.service 'Telegram-бот'
  summary_unit vpn-service-web.service 'Веб-панель'
  summary_nginx_service
  summary_unit vpn-service-nginx-guard.service 'Nginx guard'
  summary_unit vpn-service-backup.timer 'Backup timer'
  summary_unit vpn-service-reminders.timer 'Reminder timer'
  summary_unit postgresql.service 'PostgreSQL'
  summary_postgresql
  summary_xui_service
  summary_3xui
  echo
  echo "Полный лог установки:    $INSTALL_LOG"
  echo '========================================'
}

summary_running preflight "Проверка Python/systemd/disk"
preflight
summary_ok preflight "Система готова к установке"
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
  # Web uses systemd socket activation. A rollback/recovery must restore and
  # Start the socket listener before the service.
  if systemctl cat vpn-service-web.socket >/dev/null 2>&1; then
    systemctl enable vpn-service-web.socket >/dev/null 2>&1 || true
    systemctl stop vpn-service-web.service >/dev/null 2>&1 || true
    systemctl stop vpn-service-web.socket >/dev/null 2>&1 || true
    rm -f /run/vpn-service/fargovpn.sock /run/vpn-service/fargovpn.sock.stale 2>/dev/null || true
    systemctl start vpn-service-web.socket >/dev/null 2>&1 || true
    restart_unit_family vpn-service-web.service fargovpn-web.service
  else
    restart_unit_family vpn-service-web.service fargovpn-web.service
  fi
  restart_unit_family vpn-service-backup.timer
  restart_unit_family vpn-service-reminders.timer fargovpn-reminders.timer
}

on_error() {
  local code=${3:-1}
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
      if [[ -f /etc/systemd/system/vpn-service-web.service && ! -f /etc/systemd/system/vpn-service-web.socket ]]; then
        rollback_note=" Предыдущая версия восстановлена, но критический vpn-service-web.socket отсутствует."
      elif [[ -f /etc/systemd/system/vpn-service-web.socket ]] && systemctl is-active --quiet vpn-service-web.socket && systemctl is-active --quiet vpn-service-web.service; then
        rollback_note=" Предыдущая версия и systemd-службы автоматически восстановлены из $PREUPDATE_BACKUP; web socket проверен."
      else
        rollback_note=" Предыдущая версия и systemd-службы автоматически восстановлены из $PREUPDATE_BACKUP."
      fi
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

  summary_fail "${CURRENT_SUMMARY_PHASE:-failed}" "ошибка на строке $line: $command"
  write_update_status "failed" "Ошибка на строке $line: $command.$rollback_note" "${VPN_UPDATE_PROGRESS:-50}" "failed"
  echo "Установка завершилась ошибкой на строке $line: $command.$rollback_note" >&2
  print_update_summary || true
  exit "$code"
}

on_signal() {
  local signal=${1:-TERM}
  local code=143
  [[ "$signal" == "INT" ]] && code=130
  [[ "$signal" == "HUP" ]] && code=129
  trap - "$signal"
  echo "Получен сигнал $signal; обновление прерывается безопасно с кодом $code." >&2
  on_error "signal:$signal" "signal $signal" "$code"
}

trap cleanup EXIT
trap 'on_error "$LINENO" "$BASH_COMMAND" "$?"' ERR
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

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


# Профиль установки удалён: FargoVPN всегда устанавливается в полном варианте.
choose_profile() {
  PROFILE=full
}

detect_existing_nginx_worker_group() {
  local nginx_user="" nginx_group=""
  command -v nginx >/dev/null 2>&1 || return 1
  nginx_user=$(nginx -T 2>/dev/null | sed -nE 's/^[[:space:]]*user[[:space:]]+([^;[:space:]]+)([[:space:]]+[^;[:space:]]+)?;.*/\1/p' | head -n1 || true)
  if [[ -z "$nginx_user" ]]; then
    nginx_user=$(ps -eo user=,args= 2>/dev/null | sed -nE 's/^([^[:space:]]+)[[:space:]]+nginx: worker process.*/\1/p' | head -n1 || true)
  fi
  [[ -n "$nginx_user" ]] || nginx_user=www-data
  getent passwd "$nginx_user" >/dev/null 2>&1 || return 1
  nginx_group=$(id -gn "$nginx_user" 2>/dev/null || true)
  [[ -n "$nginx_group" ]] || return 1
  printf '%s\n' "$nginx_group"
}

setup_existing_nginx_route() {
  [[ "$PROFILE" == full ]] || return 0
  require_command nginx
  if ! nginx -t >>"$INSTALL_LOG" 2>&1; then
    echo "Внешний nginx: ошибка конфигурации; подробности в $INSTALL_LOG" >&2
    return 1
  fi
  # Read-only validation. The external router owns all L4/L7 configuration.
  echo 'Внешний nginx проверен; конфигурация и маршруты не изменялись.'
  echo 'Для новой установки подключите URI панели к /run/vpn-service/fargovpn.sock во внешнем nginx.'
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


if [[ -n $UPDATE_PATH ]]; then
  if OLD=$(project_dir "$UPDATE_PATH"); then :; else OLD=""; fi
  [[ -n $OLD ]] || { echo 'В указанной установке не найдены main.py и config.py.' >&2; exit 2; }
  TARGET="$OLD"
  MODE=2
  # Explicit --update-existing is intentionally non-interactive. The menu-based
  # The update path is non-interactive; only the application is replaced.
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
    ask RESTORE_DIR 'Путь к папке с резервной копией (обе части .part001/.part002)'
    [[ -d $RESTORE_DIR ]] || { echo 'Каталог резервной копии не найден.' >&2; exit 1; }
    RESTORE_PREP=$(mktemp -d /tmp/fargovpn_restore_prepare.XXXXXX)
    trap 'rm -rf "${RESTORE_PREP:-}"' EXIT
    python3 "$SRC/restore_manager.py" --prepare-dir "$RESTORE_DIR" --work-dir "$RESTORE_PREP" --json >/tmp/fargovpn_restore_prepare.json
    RESTORE="$RESTORE_PREP/backup.tar.gz"
    RESTORE_ROOT="$RESTORE_PREP/extracted/vpn_service_backup"
    echo
    echo 'Что восстановить:'
    echo '1) Только пользователи и их подписки (users/payments/referral_rewards)'
    echo '2) Полностью: базы FargoVPN/3x-ui + настройки + Xray + systemd'
    read -rp 'Выберите вариант [1-2]: ' RESTORE_CHOICE
    case "$RESTORE_CHOICE" in
      1) RESTORE_MODE=users-only ;;
      2) RESTORE_MODE=full ;;
      *) echo 'Некорректный вариант восстановления.' >&2; exit 1 ;;
    esac
    echo
    echo 'Сведения об архиве:'
    python3 - "$RESTORE_ROOT/manifest.json" <<'PY_RESTORE_INFO'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding='utf-8'))
print(f"  Версия: {data.get('version','—')}")
print(f"  Дата: {data.get('created_at','—')}")
print(f"  База FargoVPN: {data.get('application_database','—')}")
print(f"  База 3x-ui: {data.get('xui_database','—')}")
print(f"  Пользователей в архиве: {(data.get('application_row_counts') or {}).get('users','—')}")
print(f"  Конфигурация 3x-ui: {bool((data.get('components') or {}).get('xui_config'))}")
print(f"  Xray конфигурация: {bool((data.get('components') or {}).get('xray_config'))}")
print(f"  Systemd: {bool((data.get('components') or {}).get('systemd'))}")
print('  Внешний Nginx/L4: НЕ восстанавливается')
PY_RESTORE_INFO
    echo
    if [[ "$RESTORE_MODE" == "users-only" ]]; then
      echo 'Будут заменены только users, payments и referral_rewards. Аудит, безопасность, Push и служебные таблицы FargoVPN сохранятся.'
    else
      echo 'Будут заменены PostgreSQL FargoVPN, 3x-ui DB, настройки FargoVPN, /etc/x-ui, /etc/xray, VAPID и FargoVPN/3x-ui systemd units. Внешний Nginx/L4 не изменяется.'
    fi
    read -rp 'Подтвердить выбранное восстановление? Введите YES: ' RESTORE_CONFIRM
    [[ "$RESTORE_CONFIRM" == "YES" ]] || { echo 'Восстановление отменено пользователем.'; exit 0; }
    if [[ "$RESTORE_MODE" == "users-only" ]]; then
      mapfile -t LIST < <(find_old)
      if ((${#LIST[@]})); then
        echo 'Найдены установки FargoVPN для восстановления базы:'
        for index in "${!LIST[@]}"; do echo "$((index + 1))) ${LIST[$index]}"; done
        echo "$(( ${#LIST[@]} + 1 ))) Указать путь вручную"
        read -rp 'Выберите установку: ' number
        if [[ $number =~ ^[0-9]+$ ]] && ((number >= 1 && number <= ${#LIST[@]})); then
          OLD=${LIST[$((number - 1))]}
        else
          ask MANUAL 'Каталог существующей установки FargoVPN'
          OLD=$(project_dir "$MANUAL" 2>/dev/null || true)
        fi
      else
        ask MANUAL 'Каталог существующей установки FargoVPN'
        OLD=$(project_dir "$MANUAL" 2>/dev/null || true)
      fi
      [[ -n $OLD ]] || { echo 'Для восстановления только базы требуется существующая установка FargoVPN.' >&2; exit 1; }
      TARGET="$OLD"
    else
      # Full restore can be performed on a clean server after the external
      # 3x-ui installer. Reuse the standard target if it already exists.
      if [[ -d "$DEFAULT_TARGET" ]]; then TARGET="$DEFAULT_TARGET"; OLD="$DEFAULT_TARGET"; else TARGET="$DEFAULT_TARGET"; fi
      PROFILE=full
    fi
  fi
fi

# Повторная проверка после выбора фактического пути установки.
check_disk_space "$TARGET" "фактического целевого каталога" 1048576
check_disk_space "$BACKUP" "каталога резервных копий" 524288

PROFILE=full
# Для старых установок INSTALL_PROFILE читается только для совместимости.
# Любое прежнее значение, кроме полного, безопасно переводится в полный профиль.
if [[ -n $OLD && -f $OLD/config.py ]]; then
  detected_profile=$(config_literal "$OLD/config.py" INSTALL_PROFILE 2>/dev/null || true)
  if [[ -n "$detected_profile" && ${detected_profile,,} != full ]]; then
    echo "ℹ Обнаружен старый профиль установки; обновление выполняется как полный профиль без удаления существующей конфигурации."
  fi
fi

VPN_UPDATE_PROGRESS=52
summary_running dependencies "Проверяются системные пакеты"
write_update_status "installing" "Проверяются системные пакеты" "$VPN_UPDATE_PROGRESS" "dependencies"
export DEBIAN_FRONTEND=noninteractive
apt_install python3 python3-venv python3-pip curl sqlite3 ca-certificates iproute2 openssl rsync socat tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
if [[ -f "$SRC/systemd/vpn-service.logrotate" ]]; then
  install -m 0644 "$SRC/systemd/vpn-service.logrotate" /etc/logrotate.d/vpn-service
fi
require_command rsync
require_command curl
require_command ss
VPN_UPDATE_PROGRESS=58
write_update_status "installing" "Системные зависимости готовы" "$VPN_UPDATE_PROGRESS" "dependencies"
summary_ok dependencies "APT/CLI-зависимости готовы; nginx не устанавливается"
mkdir -p "$BACKUP" "$(dirname "$TARGET")" /var/lib/vpn-service/updates /var/lib/vpn-service/broadcasts /var/cache/vpn-service/chat-media /var/lib/vpn-service /var/lib/vpn-service/migration-state
chmod 700 /var/lib/vpn-service/updates /var/lib/vpn-service/broadcasts /var/cache/vpn-service/chat-media /var/lib/vpn-service/migration-state

VPN_UPDATE_PROGRESS=60
summary_running stop-services "Останавливаются службы перед безопасной заменой файлов"
write_update_status "installing" "Останавливаются службы перед безопасной заменой файлов" "$VPN_UPDATE_PROGRESS" "stop-services"
for unit in \
  fargovpn-bot fargovpn-web fargovpn-backup fargovpn-reminders \
  vpn-service-bot vpn-service-web vpn-service-backup.service vpn-service-backup.timer \
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
summary_ok stop-services "Службы освобождены для замены файлов"

TMP=$(mktemp -d)
PREUPDATE_SYSTEMD_DIR="$TMP/systemd"
mkdir -p "$PREUPDATE_SYSTEMD_DIR"
for unit_file in \
  vpn-service-bot.service vpn-service-web.service vpn-service-web.socket \
  vpn-service-backup.service vpn-service-backup.timer \
  vpn-service-reminders.service vpn-service-reminders.timer \
  fargovpn-bot.service fargovpn-web.service \
  fargovpn-backup.service fargovpn-backup.timer \
  fargovpn-reminders.service fargovpn-reminders.timer; do
  [[ ! -f /etc/systemd/system/$unit_file ]] || cp -a "/etc/systemd/system/$unit_file" "$PREUPDATE_SYSTEMD_DIR/"
done
VPN_UPDATE_PROGRESS=64
summary_running backup "Создаётся резервная копия текущей установки"
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
  # The canonical application database is data/vpn_bot.db.
  # Prefer it over any legacy root-level vpn_bot.db so migration cannot silently
  # use a stale database snapshot.
  if [[ -f "$OLD/data/vpn_bot.db" ]]; then
    cp -f "$OLD/data/vpn_bot.db" "$TMP/vpn_bot.db"
    printf '%s\n' "$(sha256sum "$TMP/vpn_bot.db")" > "$TMP/vpn_bot.db.sha256"
    echo "✓ SQLite snapshot for PostgreSQL migration: $OLD/data/vpn_bot.db"
    echo "  SHA256: $(cut -d' ' -f1 "$TMP/vpn_bot.db.sha256")"
    [[ -s "$TMP/vpn_bot.db" ]] || { echo "❌ SQLite migration snapshot is empty." >&2; exit 1; }
  elif [[ -f "$OLD/vpn_bot.db" ]]; then
    echo '⚠ Canonical data/vpn_bot.db not found; using legacy root-level vpn_bot.db as migration source.' >&2
    cp -f "$OLD/vpn_bot.db" "$TMP/vpn_bot.db"
    printf '%s\n' "$(sha256sum "$TMP/vpn_bot.db")" > "$TMP/vpn_bot.db.sha256"
  fi
fi
VPN_UPDATE_PROGRESS=68
write_update_status "installing" "Резервная копия текущей установки проверена" "$VPN_UPDATE_PROGRESS" "backup"
summary_ok backup "Резервная копия и systemd-снимок готовы"

if [[ "$MODE" == "3" ]]; then
  [[ -f "$RESTORE" && -d "$RESTORE_ROOT" ]] || { echo 'Подготовленный архив восстановления недоступен.' >&2; exit 1; }
  echo "✓ Архив восстановления склеен и целостность tar.gz проверена: $RESTORE"
fi

VPN_UPDATE_PROGRESS=73
summary_running files "Синхронизация исходников в $TARGET"
write_update_status "installing" "Файлы новой версии копируются в целевой каталог" "$VPN_UPDATE_PROGRESS" "files"

mkdir -p "$TARGET"
if [[ "$(realpath -m "$SRC")" != "$(realpath -m "$TARGET")" ]]; then
  rsync -a \
    --delete-delay \
    --exclude='config.py' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.db' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite3' \
    --exclude='config.py.before_*' \
    --exclude='config.py.corrupt.*' \
    --exclude='.venv/' \
    --exclude='data/' \
    --exclude='backups/' \
    --exclude='backup/' \
    --exclude='*.log' \
    --exclude='*.pid' \
    --exclude='*.sock' \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='.mypy_cache/' \
    --exclude='.ruff_cache/' \
    "$SRC/" "$TARGET/"
else
  echo '✓ Исходник уже находится в целевом каталоге; rsync пропущен.'
fi
# Generated caches never belong to an installed release. They are removed only
# after the pre-update backup has already been completed.
find "$TARGET" -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ruff_cache' \) -prune -exec rm -rf {} + 2>/dev/null || true
if [[ ! -f "$SRC/VERSION" ]]; then
  abort_install "в архиве отсутствует app/VERSION"
fi
install -m 0644 "$SRC/VERSION" "$TARGET/VERSION"
if [[ "$(tr -d '[:space:]' < "$TARGET/VERSION")" != "$VERSION" ]]; then
  abort_install "VERSION после синхронизации не равен $VERSION"
fi
summary_ok files "Исходники приложения и VERSION=$VERSION синхронизированы в $TARGET"
write_update_status "installing" "Файлы новой версии скопированы и VERSION=$VERSION подтверждён" "$VPN_UPDATE_PROGRESS" "files"

[[ -f $TMP/config.py ]] && cp "$TMP/config.py" "$TARGET/config.py"
[[ -f $TMP/restored_config.py ]] && cp "$TMP/restored_config.py" "$TARGET/config.py"

if [[ "$MODE" == "3" && "$RESTORE_MODE" == "full" ]]; then
  RESTORE_CONFIG_SOURCE="$RESTORE_ROOT/bot/config.py"
  [[ -f "$RESTORE_CONFIG_SOURCE" ]] || { echo 'В полном архиве не найден bot/config.py.' >&2; exit 1; }
  echo '✓ bot/config.py найден; действующий config.py остаётся нетронутым до transactional restore.'
fi

VPN_UPDATE_PROGRESS=75
summary_running python "Проверяется Python-окружение и pip-зависимости"
write_update_status "installing" "Проверяется Python-окружение" "$VPN_UPDATE_PROGRESS" "python"
if [[ ! -x "$TARGET/.venv/bin/python" ]] \
   || ! "$TARGET/.venv/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  log_step "Создаётся новое виртуальное окружение Python"
  rm -rf "$TARGET/.venv"
  python3 -m venv "$TARGET/.venv"
else
  log_step "Существующее виртуальное окружение Python исправно и будет использовано повторно"
fi
REQUIREMENTS="$TARGET/requirements.txt"
pip_install "$REQUIREMENTS"
VPN_UPDATE_PROGRESS=83
write_update_status "installing" "Python-зависимости установлены" "$VPN_UPDATE_PROGRESS" "python"
command -v tesseract >/dev/null
tesseract --list-langs 2>/dev/null | grep -qx 'rus'
tesseract --list-langs 2>/dev/null | grep -qx 'eng'
summary_ok python "venv переиспользован/создан; requirements и pip check пройдены"

CONFIG_FORCE_REBUILD=0
if [[ -f "$TARGET/config.py" ]] && ! config_is_valid "$TARGET/config.py"; then
  cp "$TARGET/config.py" "$TARGET/config.py.corrupt.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
  rm -f "$TARGET/config.py"
  CONFIG_FORCE_REBUILD=1
  echo '⚠ Обнаружен повреждённый config.py. Исходник сохранён отдельно; будет создан новый корректный Python-конфиг.' >&2
fi
if [[ ! -f "$TARGET/config.py" || $CONFIG_FORCE_REBUILD -eq 1 ]]; then
  if [[ "$MODE" == "3" && "$RESTORE_MODE" == "full" ]]; then
    # Recovery on a clean server must not ask for production credentials that
    # are already inside the archive. Seed only a neutral bootstrap config;
    # setup_postgresql.sh creates a fresh local application database, then the
    # transactional restore replaces it with the archived database.
    [[ -f "$SRC/config.example.py" ]] || { echo 'В архиве восстановления отсутствует config.example.py.' >&2; exit 1; }
    cp -f "$SRC/config.example.py" "$TARGET/config.py"
    chmod 600 "$TARGET/config.py"
    export TARGET
    "$TARGET/.venv/bin/python" - <<'PY_RECOVERY_BOOTSTRAP'
from pathlib import Path
import os, re
path = Path(os.environ["TARGET"]) / "config.py"
text = path.read_text(encoding="utf-8")
values = {
    "SERVICE_NAME": "FargoVPN",
    "BOT_TOKEN": "",
    "ADMIN_IDS": [],
    "BASE_URL": "",
    "MASTER_API_URL": "",
    "MASTER_API_TOKEN": "",
    "SUB_BASE_URL": "",
    "WEB_USERNAME": "admin",
    "WEB_PASSWORD_HASH": "",
    "WEB_SECRET_KEY": __import__("secrets").token_hex(32),
    "DATABASE_URL": "",
}
for name, value in values.items():
    line = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    text = re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"
compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")
PY_RECOVERY_BOOTSTRAP
    mkdir -p "$TARGET/data"
    echo '✓ Для чистого сервера создан нейтральный bootstrap config; учётные данные будут восстановлены из архива после подготовки локального PostgreSQL.'
  else
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

  WEB_USER='admin'
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
  if [[ -f "$TARGET/scripts/setup_postgresql.sh" ]]; then
    chmod +x "$TARGET/scripts/setup_postgresql.sh"
    log_step "Подготавливается локальный PostgreSQL"
    DATABASE_URL="$(bash "$TARGET/scripts/setup_postgresql.sh" | sed -n 's/^DATABASE_URL=//p' | tail -n 1)"
    if [[ -z "$DATABASE_URL" ]]; then abort_install 'Не удалось получить DATABASE_URL PostgreSQL'; fi
  else
    echo 'Не найден scripts/setup_postgresql.sh' >&2
    exit 1
  fi
  export TARGET BACKUP PROFILE SERVICE_NAME BOT_TOKEN ADMIN_IDS BASE_URL API_TOKEN SUB_URL PRICE PHONE BANK RECEIVER WEB_USER HASH SECRET GITHUB_OWNER GITHUB_REPO GITHUB_TOKEN DATABASE_URL
  "$TARGET/.venv/bin/python" <<'PY'
from pathlib import Path
import os
target = Path(os.environ["TARGET"])
try:
    admin_ids = sorted({int(v.strip()) for v in os.environ.get("ADMIN_IDS", "").replace(";", ",").replace(" ", ",").split(",") if v.strip()})
except ValueError:
    raise SystemExit("ADMIN_IDS должен содержать только числовые Telegram ID через запятую")
values = {
    "SERVICE_NAME": os.environ["SERVICE_NAME"], "BOT_TOKEN": os.environ["BOT_TOKEN"], "ADMIN_IDS": admin_ids,
    "SUBSCRIPTION_DAYS": 30, "BOT_WELCOME_TEXT": "", "BOT_SUPPORT_PROMPT": "",
    "FAQ_INCY_URL": "https://apps.apple.com/ru/app/incy/id6756943388", "BOT_IDENTITY_REFRESH_SECONDS": 600,
    "BOT_SYNC_INTERVAL_SECONDS": 3600, "TELEGRAM_UPDATE_LEASE_SECONDS": 600, "TELEGRAM_UPDATE_DEDUP_KEEP_DAYS": 7, "DB_PATH": str(target / "data/vpn_bot.db"),
     "DATABASE_URL": os.environ["DATABASE_URL"], "DATABASE_POOL_SIZE": 8, "DATABASE_MAX_OVERFLOW": 16,
     "BACKUP_DATABASE_TIMEOUT_SECONDS": 300,
    "BASE_URL": os.environ["BASE_URL"], "MASTER_API_URL": os.environ["BASE_URL"], "MASTER_API_TOKEN": os.environ["API_TOKEN"],
    "SUB_BASE_URL": os.environ["SUB_URL"], "PAYMENT_PRICE": int(os.environ["PRICE"]),
    "PAYMENT_PHONE": os.environ["PHONE"], "PAYMENT_BANK": os.environ["BANK"], "PAYMENT_RECEIVER": os.environ["RECEIVER"],
    "RECEIPT_OCR_ENABLED": True, "RECEIPT_AUTO_APPROVE": True, "RECEIPT_MIN_AMOUNT": 150.0, "RECEIPT_MAX_AGE_HOURS": 24,
    "RECEIPT_RECEIVER_ALIASES": "", "RECEIPT_ALLOW_MASKED_PHONE": True, "RECEIPT_TIMEZONE": "Asia/Almaty",
    "RECEIPT_OCR_LANGUAGES": "rus+eng", "RECEIPT_OCR_TIMEOUT": 20, "RECEIPT_FILTER_NAME": True,
    "RECEIPT_FILTER_PHONE": False, "RECEIPT_FILTER_AMOUNT": True, "RECEIPT_FILTER_DATE": False,
    "RECEIPT_FILTER_STATUS": True, "RECEIPT_FILTER_DUPLICATE": True,
    "WEB_HOST": "127.0.0.1", "WEB_SOCKET_PATH": "/run/vpn-service/fargovpn.sock", "WEB_SOCKET_GROUP": "www-data", "WEB_REVERSE_PROXY": True,
    "APP_LOG_PATH": "/var/log/vpn_bot.log",
    "WEB_PUBLIC_PREFIX": os.environ.get("WEB_PUBLIC_PREFIX", ""), "WEB_DOMAIN": "", "WEB_TLS_SERVER_NAME": "",
    "WEB_USERNAME": os.environ["WEB_USER"], "WEB_PASSWORD_HASH": os.environ["HASH"], "WEB_SECRET_KEY": os.environ["SECRET"],
    "WEB_COOKIE_HTTPS_ONLY": True, "WEB_SESSION_MAX_AGE_SECONDS": 28800, "BOT_PANEL_URL": os.environ.get("BOT_PANEL_URL", ""),
    "PUBLIC_PANEL_URL": os.environ.get("BOT_PANEL_URL", ""), "CABINET_ENABLED": True, "CABINET_PATH": "/cabinet",
    "CABINET_SESSION_MAX_AGE_SECONDS": 86400, "WEB_TRUST_PROXY_HEADERS": True,
    "WEB_LOGIN_MAX_ATTEMPTS": 5, "WEB_LOGIN_WINDOW_SECONDS": 900, "WEB_LOGIN_BLOCK_SECONDS": 900,
    "WEB_LOGIN_MAX_BLOCK_SECONDS": 86400, "WEB_LOGIN_SECURITY_RETENTION_DAYS": 30,
    "XUI_DB_PATH": "/etc/x-ui/x-ui.db", "XUI_PANEL_URL": os.environ["BASE_URL"].rstrip("/"),
    "XUI_INTERNAL_BASE_URL": "", "XUI_INTERNAL_AUTO_DETECT": True, "XUI_CACHE_SECONDS": 30, "XUI_VERIFY_TLS": True, "XUI_REQUEST_TIMEOUT_SECONDS": 12, "XUI_MIN_REQUEST_INTERVAL_MS": 100,
    "REMINDER_DAYS": [7,3,1,0], "REMINDER_LOCK_PATH": "/run/vpn-service-reminders.lock", "METRICS_STORE_INTERVAL_SECONDS": 60,
    "IDENTITY_IMPORT_MAX_MB": 512, "USER_EVENT_KEEP_DAYS": 365, "USER_EVENT_MAX_ROWS": 250000,
    "CHAT_MEDIA_MAX_MB": 100, "CHAT_MEDIA_CACHE_DIR": "/var/cache/vpn-service/chat-media", "CHAT_MEDIA_CACHE_DAYS": 30,
    "CHAT_MEDIA_CACHE_MAX_MB": 512, "BROADCAST_DIR": "/var/lib/vpn-service/broadcasts", "BROADCAST_MEDIA_MAX_MB": 45,
    "BROADCAST_SEND_DELAY_SECONDS": 0.04, "BROADCAST_STALE_SECONDS": 7200,
    "BACKUP_DIR": os.environ["BACKUP"], "BACKUP_KEEP_DAYS": 14, "BACKUP_INTERVAL_DAYS": 3,
    "BACKUP_RETRY_INTERVAL_SECONDS": 900, "BACKUP_PENDING_KEEP_DAYS": 30, "BACKUP_TELEGRAM": True,
    "BACKUP_TELEGRAM_PART_MB": 45, "BACKUP_INCLUDE_VENV": False, "BACKUP_LOCK_PATH": "/run/vpn-service-backup.lock", "BACKUP_FORCE_PATH": "/run/vpn-service-backup.force",
    "BACKUP_STATE_PATH": "/var/lib/vpn-service/backup-state.json",
    "BACKUP_LIVE_STATE_PATH": "/var/lib/vpn-service/backup-live.json",
    "RESTORE_STATE_PATH": "/var/lib/vpn-service/restore-state.json", "RESTORE_LOCK_PATH": "/run/vpn-service-restore.lock",
    "RESTORE_LOG_PATH": "/var/log/vpn-service-restore.log", "RESTORE_ARCHIVE_MAX_MEMBERS": 50000,
    "RESTORE_ARCHIVE_MAX_UNPACKED_MB": 4096, "RESTORE_ARCHIVE_MAX_FILE_MB": 512,
    "XUI_POSTGRES_DSN": "", "XUI_DB_ENV_FILE": "/etc/default/x-ui", "UPDATE_DIR": "/var/lib/vpn-service/updates",
    "UPDATE_PUBLISHER_USERNAME": "configured-locally", "UPDATE_IS_PUBLISHER": False,
    "GITHUB_API_BASE_URL": "https://api.github.com", "GITHUB_API_TOKEN": os.environ["GITHUB_TOKEN"],
    "GITHUB_REPOSITORY_OWNER": os.environ["GITHUB_OWNER"], "GITHUB_REPOSITORY_NAME": os.environ["GITHUB_REPO"],
    "GITHUB_TARGET_BRANCH": "main", "GITHUB_MAIN_SYNC_ENABLED": True, "GITHUB_RELEASE_TAG_PREFIX": "v",
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
fi

# On upgrade paths the initial-config block may be skipped, so DATABASE_URL
# is not guaranteed to exist in the shell environment. With `set -u`, initialize
# it before export; the Python block will fall back to config.py when empty.
DATABASE_URL="${DATABASE_URL:-}"
summary_running database "Определяется текущая БД и выполняется только необходимая миграция"
FARGOVPN_SKIP_LEGACY_SQLITE_MIGRATION=0
if [[ "$MODE" == "3" && "$RESTORE_MODE" == "full" ]]; then
  # The full backup already contains the canonical PostgreSQL dump. Do not
  # migrate the archived legacy SQLite file before restore_manager swaps it.
  FARGOVPN_SKIP_LEGACY_SQLITE_MIGRATION=1
fi
export TARGET BACKUP PROFILE TMP DATABASE_URL FARGOVPN_SKIP_LEGACY_SQLITE_MIGRATION
export FARGOVPN_DATABASE_URL="$DATABASE_URL"
"$TARGET/.venv/bin/python" <<'PY'
from pathlib import Path
import ast, hashlib, os, re, secrets, sys, shutil, subprocess
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

set_value("DB_PATH", str(Path(os.environ["TARGET"]) / "data/vpn_bot.db"))
if not literal("DATABASE_URL", ""):
    pg_setup = Path(os.environ["TARGET"]) / "scripts" / "setup_postgresql.sh"
    if pg_setup.is_file() and pg_setup.stat().st_mode & 0o111:
        import subprocess
        output = subprocess.check_output(["bash", str(pg_setup)], text=True)
        pgurl = next((line.split("=",1)[1].strip() for line in output.splitlines() if line.startswith("DATABASE_URL=")), "")
        if pgurl:
            set_value("DATABASE_URL", pgurl)
ensure("DATABASE_POOL_SIZE", 8)
ensure("DATABASE_MAX_OVERFLOW", 16)
ensure("DB_SLOW_QUERY_SECONDS", 1.0)
ensure("SLOW_OPERATION_SECONDS", 1.0)
ensure("BACKUP_DATABASE_TIMEOUT_SECONDS", 300)

# PostgreSQL-first upgrade logic.
# The migration state lives OUTSIDE $TARGET so application tree synchronization
# cannot reset it on an upgrade. The configured DATABASE_URL is authoritative:
# if the existing installation already uses PostgreSQL, never import the legacy
# SQLite file again. This prevents stale SQLite snapshots from overwriting current
# PostgreSQL data on every future upgrade.
source_db = Path(os.environ["TARGET"]) / "data" / "vpn_bot.db"
snapshot_db = Path(os.environ.get("TMP", "")) / "vpn_bot.db" if os.environ.get("TMP") else Path("")
state_root = Path("/var/lib/vpn-service/migration-state")
state_root.mkdir(parents=True, exist_ok=True)
state_file = state_root / "postgresql.json"
legacy_marker = Path(os.environ["TARGET"]) / ".postgresql_migration_complete"

import json
from datetime import datetime

def write_pg_state(status: str, dsn: str, **extra):
    payload = {
        "backend": "postgresql",
        "status": status,
        "database": dsn.rsplit("/", 1)[-1] if dsn else "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    tmp_state = state_file.with_suffix(".tmp")
    tmp_state.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_state.chmod(0o600)
    tmp_state.replace(state_file)

def probe_configured_postgresql(dsn: str):
    """Return (reachable, table_count, application_data_rows)."""
    if not dsn or not dsn.startswith("postgresql"):
        return False, 0, 0
    try:
        import psycopg
        raw_dsn = dsn.replace("+psycopg", "")
        with psycopg.connect(raw_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
                table_count = int(cur.fetchone()[0] or 0)
                if table_count == 0:
                    return True, 0, 0
                # Any application rows mean this is an initialized/live FargoVPN DB.
                total_rows = 0
                for table in ("users", "user_events", "payments", "audit_log", "metrics"):
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                    if cur.fetchone()[0] is not None:
                        cur.execute(f'SELECT count(*) FROM "{table}"')
                        total_rows += int(cur.fetchone()[0] or 0)
                return True, table_count, total_rows
    except Exception as exc:
        print(f"ℹ Existing PostgreSQL DSN is not reachable yet: {exc}", file=sys.stderr)
        return False, 0, 0

configured_dsn = str(literal("DATABASE_URL", "") or "").strip()
pg_reachable, pg_tables, pg_rows = probe_configured_postgresql(configured_dsn)

if configured_dsn.startswith("postgresql") and pg_reachable and pg_tables > 0:
    # A reachable, non-empty PostgreSQL database is live application state.
    # Never overwrite it with a legacy SQLite snapshot during an upgrade.
    set_value("DATABASE_URL", configured_dsn)
    write_pg_state(
        "existing_postgresql_skipped_sqlite_import",
        configured_dsn,
        table_count=pg_tables,
        sample_application_rows=pg_rows,
        sqlite_present=source_db.is_file() or snapshot_db.is_file(),
    )
    print(
        f"POSTGRESQL_DETECTED=1 tables={pg_tables} sample_rows={pg_rows}",
        file=sys.stderr,
    )
    print(
        "✓ PostgreSQL уже содержит данные этой установки; повторная SQLite → PostgreSQL конвертация ПРОПУЩЕНА.",
        file=sys.stderr,
    )
elif configured_dsn.startswith("postgresql") and pg_reachable and pg_tables == 0:
    # A clean/empty PostgreSQL database is not evidence that migration already
    # happened. On an upgrade with a legacy SQLite file, import it exactly once.
    # On a genuinely new installation without SQLite, simply keep the empty DB
    # and let init_db.py create the current schema.
    set_value("DATABASE_URL", configured_dsn)
    has_legacy_sqlite = source_db.is_file() or snapshot_db.is_file()
    if has_legacy_sqlite and not legacy_marker.is_file():
        print("POSTGRESQL_DETECTED=1 tables=0 sample_rows=0; обнаружена legacy SQLite база — выполняется однократная миграция.", file=sys.stderr)
        dsn = configured_dsn
        if snapshot_db.is_file():
            source_db = snapshot_db
        elif source_db.is_file():
            pass
        else:
            source_db = Path("")
        if not source_db.is_file():
            raise SystemExit("Обнаружена пустая PostgreSQL, но legacy SQLite база для миграции не найдена.")
        try:
            import psycopg
            with psycopg.connect(dsn.replace("+psycopg", ""), connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
                    existing_tables = int(cur.fetchone()[0] or 0)
                    if existing_tables:
                        raise SystemExit(f"PostgreSQL уже содержит {existing_tables} таблиц; автоматический импорт SQLite остановлен во избежание потери актуальных данных.")
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"PostgreSQL недоступен перед миграцией: {exc}") from exc
        manifest = Path(os.environ["TARGET"]) / "data" / "migration_manifest.json"
        report = Path(os.environ["TARGET"]) / "data" / "migration_report.json"
        migration_tool = Path(os.environ["TARGET"]) / "migration_tool.py"
        durable_root = Path(os.environ.get("BACKUP", "/var/backups/vpn-service")) / "migration-diagnostics"
        durable_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        durable_report = durable_root / f"migration_report_{stamp}.json"
        durable_manifest = durable_root / f"migration_manifest_{stamp}.json"
        source_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()
        command = [sys.executable, str(migration_tool), "--sqlite", str(source_db), "--dsn", dsn, "--manifest", str(manifest), "--report", str(report), "--failure-report", str(durable_report), "--reset-target"]
        result = subprocess.run(command, cwd=os.environ["TARGET"], text=True)
        if manifest.is_file():
            try: shutil.copy2(manifest, durable_manifest)
            except Exception: pass
        if result.returncode != 0:
            write_pg_state("migration_failed", dsn, sqlite_sha256=source_sha)
            raise SystemExit("SQLite → PostgreSQL завершился ошибкой. Старые службы не запускаются.")
        write_pg_state("sqlite_migrated_to_postgresql", dsn, sqlite_sha256=source_sha, migration_report=str(durable_report))
        legacy_marker.parent.mkdir(parents=True, exist_ok=True)
        legacy_marker.write_text(f"migrated_at={datetime.now().isoformat(timespec='seconds')}\nsource_sha256={source_sha}\n", encoding="utf-8")
        legacy_marker.chmod(0o600)
        print("✓ SQLite → PostgreSQL миграция завершена; состояние сохранено вне каталога приложения.", file=sys.stderr)
    else:
        write_pg_state("fresh_postgresql_ready", configured_dsn, table_count=0, sample_application_rows=0, sqlite_present=False)
        print("POSTGRESQL_DETECTED=1 tables=0 sample_rows=0", file=sys.stderr)
        print("✓ Пустая PostgreSQL подготовлена для новой установки; SQLite → PostgreSQL миграция не требуется.", file=sys.stderr)
else:
    # No usable PostgreSQL backend is configured. This is the legacy SQLite path:
    # prepare PostgreSQL (or reuse an explicitly configured but empty PostgreSQL
    # database), then perform exactly one migration and record it outside $TARGET.
    dsn = configured_dsn if (configured_dsn.startswith("postgresql") and pg_reachable and pg_tables == 0) else ""
    if not dsn:
        pg_setup = Path(os.environ["TARGET"]) / "scripts" / "setup_postgresql.sh"
        if not pg_setup.is_file():
            raise SystemExit("Не найден scripts/setup_postgresql.sh; PostgreSQL миграция невозможна.")
        try:
            pg_setup.chmod(pg_setup.stat().st_mode | 0o700)
        except OSError:
            pass
        import subprocess
        try:
            output = subprocess.check_output(
                ["bash", str(pg_setup)],
                text=True,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "Не удалось подготовить PostgreSQL перед миграцией.\n" + (exc.output or "")
            ) from exc
        dsn = next(
            (line.split("=", 1)[1].strip()
             for line in output.splitlines()
             if line.startswith("DATABASE_URL=")),
            "",
        )
        if not dsn:
            raise SystemExit("setup_postgresql.sh не вернул DATABASE_URL.")
        print(f"POSTGRES_DSN_PREPARED={dsn.split('@')[-1]}")

    set_value("DATABASE_URL", dsn)
    os.environ["FARGOVPN_DATABASE_URL"] = dsn
    os.environ["DATABASE_URL"] = dsn

    if os.environ.get("FARGOVPN_SKIP_LEGACY_SQLITE_MIGRATION") == "1":
        print("✓ Полное восстановление: предварительная SQLite → PostgreSQL миграция пропущена; будет восстановлен проверенный PostgreSQL dump.", file=sys.stderr)
    else:
        if snapshot_db.is_file():
            source_db = snapshot_db
        elif not source_db.is_file():
            source_db = Path("")
        if not source_db.is_file():
            raise SystemExit(
                "PostgreSQL backend не настроен и не найден legacy SQLite database: "
                f"{Path(os.environ['TARGET']) / 'data' / 'vpn_bot.db'}"
            )

        # Refuse an accidental import into a non-empty configured PostgreSQL DB. A
        # non-empty DB is already application state and must be preserved.
        try:
            import psycopg
            with psycopg.connect(dsn.replace("+psycopg", ""), connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
                    existing_tables = int(cur.fetchone()[0] or 0)
                    if existing_tables:
                        write_pg_state("existing_postgresql_refused_auto_import", dsn, table_count=existing_tables)
                        raise SystemExit(
                            f"PostgreSQL уже содержит {existing_tables} таблиц; автоматический импорт SQLite остановлен во избежание потери актуальных данных."
                        )
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"PostgreSQL недоступен перед миграцией: {exc}") from exc

        import subprocess
        manifest = Path(os.environ["TARGET"]) / "data" / "migration_manifest.json"
        report = Path(os.environ["TARGET"]) / "data" / "migration_report.json"
        migration_tool = Path(os.environ["TARGET"]) / "migration_tool.py"
        durable_root = Path(os.environ.get("BACKUP", "/var/backups/vpn-service")) / "migration-diagnostics"
        durable_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        durable_report = durable_root / f"migration_report_{stamp}.json"
        durable_manifest = durable_root / f"migration_manifest_{stamp}.json"
        source_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()
        print(f"SQLITE_SOURCE={source_db}")
        print(f"SQLITE_SHA256={source_sha}")
        print(f"MIGRATION_REPORT_DURABLE={durable_report}")
        print(f"MIGRATION_MANIFEST_DURABLE={durable_manifest}")
        command = [
            sys.executable, str(migration_tool),
            "--sqlite", str(source_db),
            "--dsn", dsn,
            "--manifest", str(manifest),
            "--report", str(report),
            "--failure-report", str(durable_report),
            "--reset-target",
        ]
        result = subprocess.run(command, cwd=os.environ["TARGET"], text=True)
        if manifest.is_file():
            try:
                import shutil
                shutil.copy2(manifest, durable_manifest)
            except Exception:
                pass
        if result.returncode != 0:
            write_pg_state("migration_failed", dsn, sqlite_sha256=source_sha)
            print(f"❌ Подробный отчёт миграции сохранён: {durable_report}", file=sys.stderr)
            raise SystemExit("SQLite → PostgreSQL завершился ошибкой. Старые службы не запускаются.")

        write_pg_state(
            "sqlite_migrated_to_postgresql",
            dsn,
            sqlite_sha256=source_sha,
            migration_report=str(durable_report),
        )
        for path_to_protect in (manifest, report, durable_report, durable_manifest, state_file):
            try:
                path_to_protect.chmod(0o600)
            except OSError:
                pass
        print("✓ SQLite → PostgreSQL миграция завершена; состояние сохранено вне каталога приложения.", file=sys.stderr)
ensure("PUBLIC_PANEL_URL", str(literal("BOT_PANEL_URL", "") or ""))
set_value("BACKUP_DIR", os.environ["BACKUP"])
ensure("SERVICE_NAME", "FargoVPN")
ensure("SUBSCRIPTION_DAYS", 30)
ensure("BOT_WELCOME_TEXT", "")
ensure("BOT_SUPPORT_PROMPT", "")
ensure("FAQ_INCY_URL", "https://apps.apple.com/ru/app/incy/id6756943388")
ensure("BOT_IDENTITY_REFRESH_SECONDS", 600)
ensure("BOT_SYNC_INTERVAL_SECONDS", 3600)
ensure("TELEGRAM_UPDATE_LEASE_SECONDS", 600)
ensure("TELEGRAM_UPDATE_DEDUP_KEEP_DAYS", 7)
ensure("BACKUP_TELEGRAM", True)
ensure("BACKUP_TELEGRAM_PART_MB", 45)
ensure("BACKUP_KEEP_DAYS", 14)
ensure("BACKUP_INTERVAL_DAYS", 3)
ensure("BACKUP_RETRY_INTERVAL_SECONDS", 900)
ensure("BACKUP_PENDING_KEEP_DAYS", 30)
ensure("BACKUP_INCLUDE_VENV", False)
ensure("BACKUP_LOCK_PATH", "/run/vpn-service-backup.lock")
ensure("BACKUP_FORCE_PATH", "/run/vpn-service-backup.force")
ensure("BACKUP_STATE_PATH", "/var/lib/vpn-service/backup-state.json")
ensure("BACKUP_LIVE_STATE_PATH", "/var/lib/vpn-service/backup-live.json")
ensure("RESTORE_STATE_PATH", "/var/lib/vpn-service/restore-state.json")
ensure("RESTORE_LOCK_PATH", "/run/vpn-service-restore.lock")
ensure("RESTORE_LOG_PATH", "/var/log/vpn-service-restore.log")
ensure("RESTORE_ARCHIVE_MAX_MEMBERS", 50000)
ensure("RESTORE_ARCHIVE_MAX_UNPACKED_MB", 4096)
ensure("RESTORE_ARCHIVE_MAX_FILE_MB", 512)
ensure("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
ensure("XUI_INTERNAL_BASE_URL", "")
ensure("XUI_INTERNAL_AUTO_DETECT", True)
ensure("XUI_CACHE_SECONDS", 30)
ensure("XUI_VERIFY_TLS", True)
ensure("XUI_REQUEST_TIMEOUT_SECONDS", 12)
ensure("XUI_MIN_REQUEST_INTERVAL_MS", 100)
ensure("XUI_INBOUND_CACHE_SECONDS", 60)
ensure("XUI_MANAGED_INBOUND_IDS", [])
ensure("XUI_MANAGED_PROTOCOLS", [])
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
nginx_group = ""
try:
    if shutil.which("nginx"):
        probe = subprocess.run(["nginx", "-T"], capture_output=True, text=True, timeout=20, check=False)
        merged = (probe.stdout or "") + "\n" + (probe.stderr or "")
        match = re.search(r"(?m)^\s*user\s+([^;\s]+)", merged)
        if match:
            import grp, pwd
            try:
                nginx_group = grp.getgrgid(pwd.getpwnam(match.group(1)).pw_gid).gr_name
            except Exception:
                nginx_group = ""
except Exception:
    nginx_group = ""
if os.environ["PROFILE"] == "full":
    if not nginx_group:
        raise SystemExit("Не удалось определить группу рабочего пользователя внешнего Nginx")
    # The previous installation may have stored www-data. Recalculate on
    # every upgrade so systemd SocketGroup matches the actual nginx worker.
    set_value("WEB_SOCKET_GROUP", nginx_group)
else:
    ensure("WEB_SOCKET_GROUP", nginx_group or "www-data")
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
ensure("XUI_POSTGRES_DSN", "")
ensure("XUI_DB_ENV_FILE", "/etc/default/x-ui")
ensure("UPDATE_DIR", "/var/lib/vpn-service/updates")
ensure("UPDATE_PUBLISHER_USERNAME", "configured-locally")
ensure("GITHUB_API_BASE_URL", "https://api.github.com")
ensure("GITHUB_API_TOKEN", "")
ensure("GITHUB_REPOSITORY_OWNER", "")
ensure("GITHUB_REPOSITORY_NAME", "FargoVPN")
ensure("GITHUB_TARGET_BRANCH", "main")
ensure("GITHUB_RELEASE_TAG_PREFIX", "v")
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
# Never continue with a corrupted config.py.
compile_source = path.read_text(encoding="utf-8")
try:
    compile(compile_source, str(path), "exec")
except SyntaxError as exc:
    first_lines = "\n".join(compile_source.splitlines()[:12])
    raise SystemExit(f"Сгенерированный config.py не является корректным Python ({exc}).\nПервые строки файла:\n{first_lines}")
PY
summary_ok database "PostgreSQL определён; повторная SQLite→PostgreSQL миграция пропущена при существующей live-БД или выполнена один раз"


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

if [[ $PROFILE == full ]]; then
  SOCKET_GROUP=$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_GROUP", "www-data")).strip() or "www-data")')
  getent group "$SOCKET_GROUP" >/dev/null 2>&1 || { echo "Не найдена группа WEB_SOCKET_GROUP: $SOCKET_GROUP" >&2; exit 1; }
fi
VPN_UPDATE_PROGRESS=86
write_update_status "migrating" "Проверяется конфигурация и обновляется база" "$VPN_UPDATE_PROGRESS" "database"
log_step "Проверка конфигурации и Python-модулей"
chmod 0600 "$TARGET/config.py"
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
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$TARGET /var/log /var/lib/vpn-service /var/backups/vpn-service /run
Restart=on-failure
RestartSec=30
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

  if ! cat > /etc/systemd/system/vpn-service-web.socket <<EOF_UNIT
[Unit]
Description=Unix socket for FargoVPN web panel

[Socket]
ListenStream=$SOCKET_PATH
SocketUser=root
SocketGroup=$SOCKET_GROUP
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
Requires=vpn-service-web.socket
After=network-online.target vpn-service-web.socket
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
else
  systemctl disable --now vpn-service-web.service >/dev/null 2>&1 || true
  systemctl disable --now vpn-service-web.socket >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vpn-service-web.service /etc/systemd/system/vpn-service-web.socket
fi

# Retire the old configuration-writing guard. Existing proxy locations remain intact.
systemctl disable --now vpn-service-nginx-guard.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/vpn-service-nginx-guard.service

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
ExecStart=$TARGET/.venv/bin/python $TARGET/backup.py --service
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
OnCalendar=*-*-* 03:30:00
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
summary_running services "Проверяются systemd unit-файлы"
VPN_UPDATE_PROGRESS=94
for legacy_unit in \
  fargovpn-backup.service fargovpn-backup.timer \
  vpn-bot-backup.service vpn-bot-backup.timer \
  vpn_bot_backup.service vpn_bot_backup.timer; do
  systemctl disable --now "$legacy_unit" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$legacy_unit"
done
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
  )
  [[ ! -f /etc/systemd/system/vpn-service-nginx-guard.service ]] || VERIFY_UNITS+=(/etc/systemd/system/vpn-service-nginx-guard.service)
fi
if ! systemd-analyze verify "${VERIFY_UNITS[@]}"; then
  abort_install 'Проверка unit-файлов systemd завершилась ошибкой. Службы не запущены.'
fi
summary_ok services "systemd unit-файлы записаны и проверены"

VPN_UPDATE_PROGRESS=96
summary_running restart "Перезапускаются службы"
write_update_status "restarting" "Перезапускаются службы" "$VPN_UPDATE_PROGRESS" "restart"
if [[ $PROFILE == full ]]; then
  if [[ ! -f /etc/systemd/system/vpn-service-web.service ]]; then abort_install 'vpn-service-web.service не создан'; fi
  if [[ ! -f /etc/systemd/system/vpn-service-web.socket ]]; then abort_install 'vpn-service-web.socket не создан'; fi
fi
systemctl enable vpn-service-bot.service vpn-service-backup.timer vpn-service-reminders.timer
# `enable` only affects boot-time activation and does not start an already
# stopped unit. Start the bot explicitly when a token is configured; with an
# empty token run_bot.sh exits cleanly and the installation remains usable.
BOT_READY_FOR_RESTART=$(target_python -c 'import config; print("1" if str(getattr(config, "BOT_TOKEN", "")).strip() else "0")')
if [[ "$BOT_READY_FOR_RESTART" == "1" ]]; then
  if ! systemctl restart vpn-service-bot.service; then
    summary_unit vpn-service-bot.service 'Telegram-бот' >&2
    abort_install 'Не удалось запустить vpn-service-bot.service после обновления'
  fi
fi
if [[ $PROFILE == full ]]; then
  systemctl enable vpn-service-web.socket
  systemctl enable vpn-service-web.service
  # Чистая socket-activation транзакция: старый listener закрывается полностью,
  # затем создаётся новый и только после этого запускается web service.
  systemctl stop vpn-service-web.service vpn-service-web.socket >/dev/null 2>&1 || true
  rm -f "$SOCKET_PATH".stale "$SOCKET_PATH" 2>/dev/null || true
  systemctl start vpn-service-web.socket
  if ! systemctl is-active --quiet vpn-service-web.socket; then
    echo "  state=$(systemctl show vpn-service-web.socket -p ActiveState --value 2>/dev/null || true)" >&2
    echo "  substate=$(systemctl show vpn-service-web.socket -p SubState --value 2>/dev/null || true)" >&2
    abort_install 'vpn-service-web.socket не запустился'
  fi
  systemctl start vpn-service-web.service
  WEB_ACTIVE=0
  for _ in $(seq 1 15); do
    if systemctl is-active --quiet vpn-service-web.service; then
      WEB_ACTIVE=1
      break
    fi
    WEB_STATE=$(systemctl show vpn-service-web.service -p ActiveState --value 2>/dev/null || true)
    WEB_RESULT=$(systemctl show vpn-service-web.service -p Result --value 2>/dev/null || true)
    if [[ "$WEB_STATE" == "failed" || "$WEB_RESULT" == "exit-code" ]]; then
      break
    fi
    sleep 1
  done
  if [[ $WEB_ACTIVE -ne 1 ]]; then
    echo 'Критическая ошибка: vpn-service-web.service не перешёл в active после запуска.' >&2
    service_failure_details vpn-service-web.service /var/log/vpn_bot.log
    abort_install 'vpn-service-web.service не перешёл в active после запуска'
  fi
fi
if [[ $PROFILE == full && "$MODE" != "3" ]]; then
  summary_running nginx "Проверяется и подключается уже установленный внешний Nginx"
  setup_existing_nginx_route
  summary_ok nginx "Внешний Nginx используется без установки/перезаписи L4-конфигурации"
  if [[ -f /etc/systemd/system/vpn-service-nginx-guard.service ]]; then
    systemctl enable vpn-service-nginx-guard.service >/dev/null 2>&1
    systemctl restart vpn-service-nginx-guard.service
    if ! systemctl is-active --quiet vpn-service-nginx-guard.service; then
      echo "  state=$(systemctl show vpn-service-nginx-guard.service -p ActiveState --value 2>/dev/null || true)" >&2
      echo "  substate=$(systemctl show vpn-service-nginx-guard.service -p SubState --value 2>/dev/null || true)" >&2
      echo "  result=$(systemctl show vpn-service-nginx-guard.service -p Result --value 2>/dev/null || true)" >&2
      abort_install 'guard внешнего Nginx не запустился'
    fi
  fi
fi
systemctl restart vpn-service-backup.timer
systemctl restart vpn-service-reminders.timer
summary_ok restart "Бот, web socket/service и таймеры запущены/проверяются; Nginx внешний"

VPN_UPDATE_PROGRESS=98
summary_running health "Проверяется запуск служб, health endpoint и версия панели"
write_update_status "health-check" "Проверяется запуск бота и веб-панели" "$VPN_UPDATE_PROGRESS" "health"
BOT_READY=$(target_python -c 'import config; print("1" if str(getattr(config, "BOT_TOKEN", "")).strip() else "0")')
if [[ "$BOT_READY" == "1" ]]; then
  if ! systemctl is-active --quiet vpn-service-bot.service; then
    summary_unit vpn-service-bot.service 'Telegram-бот' >&2
    echo 'Критическая ошибка: vpn-service-bot.service не active.' >&2
    service_failure_details vpn-service-bot.service /var/log/vpn_bot.log
    abort_install 'vpn-service-bot.service не active'
  fi
  sleep 3
  BOT_RESTARTS=$(systemctl show vpn-service-bot.service -p NRestarts --value 2>/dev/null || echo 0)
  BOT_RESTARTS=${BOT_RESTARTS:-0}
  if [[ "$BOT_RESTARTS" =~ ^[0-9]+$ ]] && (( BOT_RESTARTS > 0 )); then
    summary_unit vpn-service-bot.service 'Telegram-бот' >&2
    abort_install "бот начал автоматические перезапуски сразу после обновления (NRestarts=$BOT_RESTARTS)"
  fi
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
    summary_unit vpn-service-web.socket 'Web socket' >&2
    echo "Состояние vpn-service-web.service:" >&2
    summary_unit vpn-service-web.service 'Web service' >&2
    echo "Сведения о Unix socket:" >&2
    ls -l "$SOCKET_PATH" >&2 2>/dev/null || true
    stat -c 'path=%n type=%F mode=%a uid=%u gid=%g size=%s' "$SOCKET_PATH" >&2 2>/dev/null || true
    ss -xlpn 2>/dev/null | grep -F "$SOCKET_PATH" >&2 || true
    echo "Проверка VERSION на диске:" >&2
    cat "$TARGET/VERSION" >&2 || true
    echo "Подробности сохранены в $INSTALL_LOG." >&2
    abort_install "health-check веб-панели не пройден"
  fi
  NGINX_WORKER_USER=$(nginx -T 2>/dev/null | sed -nE 's/^[[:space:]]*user[[:space:]]+([^;[:space:]]+)([[:space:]]+[^;[:space:]]+)?;.*/\1/p' | head -n1 || true)
  [[ -n "$NGINX_WORKER_USER" ]] || abort_install 'не удалось определить рабочего пользователя Nginx для проверки socket'
  NGINX_HEALTH=$(runuser -u "$NGINX_WORKER_USER" -- curl --unix-socket "$SOCKET_PATH" -fsS --connect-timeout 2 --max-time 5 http://localhost/health 2>&1) || {
    echo "Nginx user=$NGINX_WORKER_USER, socket group=$SOCKET_GROUP" >&2
    stat -c 'socket=%n mode=%a owner=%U group=%G' "$SOCKET_PATH" >&2 || true
    echo "Проверка socket от имени Nginx: $NGINX_HEALTH" >&2
    abort_install 'рабочий пользователь Nginx не может подключиться к веб-панели'
  }
  [[ "$NGINX_HEALTH" == OK* ]] || abort_install 'веб-панель не ответила рабочему пользователю Nginx'
  if [[ ! -f "$TARGET/VERSION" ]]; then abort_install 'VERSION на диске не найден после запуска веб-панели'; fi
  INSTALLED_VERSION=$(tr -d '[:space:]' < "$TARGET/VERSION")
  if [[ "$INSTALLED_VERSION" != "$VERSION" ]]; then abort_install "Веб-панель запустилась, но VERSION=$INSTALLED_VERSION вместо $VERSION"; fi

  SW_BODY=$(curl --unix-socket "$SOCKET_PATH" -fsS --connect-timeout 2 --max-time 5 http://localhost/service-worker.js 2>/dev/null || true)
  if [[ -z "$SW_BODY" ]] || ! grep -Fq "const VERSION = '$VERSION';" <<<"$SW_BODY"; then
    abort_install "Service Worker не содержит актуальную версию $VERSION после установки"
  fi

  # The installer explicitly allows a fresh installation to leave 3x-ui
  # credentials empty and configure them later from the web panel. Therefore
  # the live API smoke-check is mandatory when either authentication mode is configured.
  XUI_TOKEN_READY=$(target_python -c 'import config; print("1" if (str(getattr(config, "MASTER_API_TOKEN", "") or "").strip() or (getattr(config, "XUI_USERNAME", "") and getattr(config, "XUI_PASSWORD", ""))) else "0")')
  if [[ "$XUI_TOKEN_READY" == "1" ]]; then
    # FargoVPN depends on the live 3x-ui API for users, traffic, online state
    # and server status. Verify the same integration layer used by the web panel.
    XUI_SMOKE=$(target_python - <<'PY_XUI_SMOKE'
from services.xui_api import request_json_sync, fetch_snapshot_sync
import json
snapshot = fetch_snapshot_sync(force=True)
if snapshot.get("stale"):
    raise SystemExit("3x-ui: " + str(snapshot.get("error") or "клиенты недоступны"))
results = {"clients": True}
for name, method, path in (
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
      echo "$XUI_SMOKE" >&2
      abort_install 'FargoVPN не получил корректный ответ от 3x-ui API' 
    }
    echo "$XUI_SMOKE"
  else
    echo 'ℹ Проверка 3x-ui API пропущена: API-токен 3x-ui пока не настроен.'
    echo '  Это допустимо для новой установки. Токен можно указать позже в Настройки → 3x-ui.'
  fi

fi

if [[ "$MODE" == "3" ]]; then
  VPN_UPDATE_PROGRESS=99
  write_update_status "restoring" "Восстанавливается проверенный архив: $RESTORE_MODE" "$VPN_UPDATE_PROGRESS" "restore"
  echo
  echo 'Перед восстановлением автоматически создаётся страховочный полный бэкап текущего состояния.'
  echo 'При сбое restore_manager выполняет откат баз, конфигурации и systemd.'
  RESTORE_SAFETY_JSON=$(target_python "$TARGET/restore_manager.py" --safety-backup --json) || abort_install 'не удалось создать/проверить страховочный бэкап перед восстановлением'
  RESTORE_SAFETY_ARCHIVE=$(printf '%s\n' "$RESTORE_SAFETY_JSON" | target_python -c 'import json,sys; print(json.load(sys.stdin)["archive"])')
  [[ -f "$RESTORE_SAFETY_ARCHIVE" ]] || abort_install 'страховочный архив перед восстановлением не найден'
  echo "✓ Страховочный архив: $RESTORE_SAFETY_ARCHIVE"
  # Do not copy/seed archive config here: restore_manager must snapshot the
  # current configuration first, then apply the archive transactionally.
  if [[ "$RESTORE_MODE" == "users-only" ]]; then
    target_python "$TARGET/restore_manager.py" --users-only "$RESTORE" --safety-archive "$RESTORE_SAFETY_ARCHIVE" --no-safety
  else
    target_python "$TARGET/restore_manager.py" --full "$RESTORE" --safety-archive "$RESTORE_SAFETY_ARCHIVE" --no-safety
  fi
  if [[ "$RESTORE_MODE" == "full" && "$PROFILE" == "full" ]]; then
    # Full restore restores WEB_PUBLIC_PREFIX/WEB_DOMAIN from the archive.
    # Re-sync only FargoVPN's dedicated location in the already installed
    # external Nginx; the external L4/L7 configuration is never replaced.
    setup_existing_nginx_route
    if [[ -f /etc/systemd/system/vpn-service-nginx-guard.service ]]; then
      systemctl restart vpn-service-nginx-guard.service
    fi
  fi
  echo '✓ Восстановление завершено; выполняется повторная проверка живых служб.'
  systemctl reset-failed >/dev/null 2>&1 || true
  BOT_READY=$(target_python -c 'import config; print("1" if str(getattr(config, "BOT_TOKEN", "")).strip() else "0")')
  if [[ "$BOT_READY" == "1" ]]; then
    if ! systemctl is-active --quiet vpn-service-bot.service; then summary_unit vpn-service-bot.service 'Telegram-бот' >&2; abort_install 'бот после восстановления не active'; fi
  fi
  if [[ "$PROFILE" == "full" ]]; then
    SOCKET_PATH=$(target_python -c 'import config; print(str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")))')
    HEALTH_BODY=$(curl --unix-socket "$SOCKET_PATH" -fsS --connect-timeout 2 --max-time 5 http://localhost/health 2>/dev/null || true)
    if [[ "$HEALTH_BODY" != OK* ]]; then abort_install 'веб-панель после восстановления не прошла health-check'; fi
    if ! systemctl is-active --quiet vpn-service-web.service; then abort_install 'vpn-service-web.service после восстановления не active'; fi
    XUI_SERVICE_OK=0
    for XUI_UNIT in x-ui.service 3x-ui.service; do
      if systemctl cat "$XUI_UNIT" >/dev/null 2>&1 && systemctl is-active --quiet "$XUI_UNIT"; then
        XUI_SERVICE_OK=1
        break
      fi
    done
    if [[ $XUI_SERVICE_OK -ne 1 ]]; then abort_install 'служба 3x-ui после восстановления не active'; fi
  fi
fi

systemctl reset-failed >/dev/null 2>&1 || true
PROFILE=full
PROFILE_STATUS="Полная установка"
PANEL_PUBLIC_URL=$(target_python "$TARGET/nginx_panel_guard.py" --print-url 2>/dev/null || true)
STATUS_DETAIL="$PROFILE_STATUS установлен; проверки служб пройдены"
if [[ -n "$PANEL_PUBLIC_URL" ]]; then
  STATUS_DETAIL+="; Веб-панель: $PANEL_PUBLIC_URL"
fi
# When this release is installed from the administrator panel, the previous
# publisher may have added a compatibility VERSION at repository root. After the
# new publisher is installed, repair main from the complete package root so the
# public tree immediately becomes the intended minimal structure.
if [[ -f "$TARGET/config.py" ]]; then
  GITHUB_PUBLISHER_READY=$(target_python -c 'import config; print("1" if str(getattr(config, "GITHUB_API_TOKEN", "") or "").strip() and bool(getattr(config, "GITHUB_MAIN_SYNC_ENABLED", False)) else "0")' 2>/dev/null || echo 0)
  if [[ "$GITHUB_PUBLISHER_READY" == "1" && -f "$TARGET/update_manager.py" ]]; then
    write_update_status "publishing" "Проверяется и очищается публичный GitHub main" "99" "github"
    if ! target_python "$TARGET/update_manager.py" --sync-main-directory "$(dirname "$SRC")" --version "$VERSION" --json; then
      echo '⚠️ Не удалось автоматически синхронизировать GitHub main после установки. Установка приложения завершена, но репозиторий требует повторной публикации.' >&2
    else
      echo '✓ GitHub main синхронизирован по новой минимальной структуре.'
    fi
  fi
fi

summary_ok health "Финальный health-check завершён; версия $VERSION активна"
write_update_status "completed" "$STATUS_DETAIL" "100" "complete"
print_update_summary
echo "Установка/обновление завершено: версия $VERSION"
