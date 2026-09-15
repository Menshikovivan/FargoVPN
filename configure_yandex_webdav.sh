#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo 'Запустите скрипт через sudo или от пользователя root.' >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG="$APP_DIR/config.py"
DEFAULT_MOUNT="/mnt/yandex-disk"
WEBDAV_URL="https://webdav.yandex.ru"
DAVFS_CONF="/etc/davfs2/davfs2.conf"
ACTION=${1:-setup}

config_string() {
  local name=$1 default=$2
  python3 - "$CONFIG" "$name" "$default" <<'PY'
import ast, pathlib, sys
path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
default = sys.argv[3]
try:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                print(value)
                raise SystemExit
except Exception:
    pass
print(default)
PY
}

tune_davfs_conf() {
  local mount_point=$1
  mkdir -p /etc/davfs2
  touch "$DAVFS_CONF"
  chmod 644 "$DAVFS_CONF"
  python3 - "$DAVFS_CONF" "$mount_point" <<'PY_DAVFS'
from pathlib import Path
import os, re, sys, tempfile

path = Path(sys.argv[1])
mount_point = sys.argv[2]
header = f"[{mount_point}]"
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()
start = None
for index, line in enumerate(lines):
    if line.strip() == header:
        start = index
        break

if start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend([header, "delay_upload 0"])
else:
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    found = False
    rebuilt = lines[: start + 1]
    for line in lines[start + 1 : end]:
        if re.match(r"^\s*delay_upload\s+", line):
            if not found:
                rebuilt.append("delay_upload 0")
                found = True
            continue
        rebuilt.append(line)
    if not found:
        rebuilt.append("delay_upload 0")
    rebuilt.extend(lines[end:])
    lines = rebuilt

payload = "\n".join(lines).rstrip() + "\n"
fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY_DAVFS
}

update_config() {
  local mount_point=$1 enabled=$2
  [[ -f $CONFIG ]] || return 0
  python3 - "$CONFIG" "$mount_point" "$enabled" <<'PY'
from pathlib import Path
import re, sys
path = Path(sys.argv[1])
mount_point = sys.argv[2]
enabled = sys.argv[3] == "true"
text = path.read_text(encoding="utf-8")
values = {
    "YANDEX_DISK_ENABLED": enabled,
    "YANDEX_DISK_MODE": "local",
    "YANDEX_LOCAL_PATH": mount_point,
    "YANDEX_LOCAL_REQUIRE_MOUNT": True,
}
for name, value in values.items():
    line = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    text = re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"
path.write_text(text, encoding="utf-8")
PY
  chmod 600 "$CONFIG"
}

status() {
  local mount_point
  mount_point=$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")
  echo "Точка монтирования: $mount_point"
  if mountpoint -q "$mount_point"; then
    echo 'Состояние: подключено'
    findmnt "$mount_point" || true
    df -h "$mount_point" || true
  else
    echo 'Состояние: не подключено'
    return 1
  fi
}

setup() {
  command -v mount.davfs >/dev/null 2>&1 || {
    echo 'Пакет davfs2 не установлен. Выполните: apt-get install davfs2' >&2
    exit 1
  }

  local current mount_point login app_password stamp secrets_tmp fstab_tmp
  current=$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")
  read -rp "Логин Яндекса: " login
  read -rp "Точка монтирования [$current]: " mount_point
  mount_point=${mount_point:-$current}
  read -rsp 'Пароль приложения Яндекса (не основной пароль аккаунта): ' app_password
  echo

  [[ -n $login && $login != *[[:space:]]* ]] || { echo 'Логин не должен быть пустым или содержать пробелы.' >&2; exit 2; }
  [[ -n $app_password && $app_password != *[[:space:]]* ]] || { echo 'Пароль приложения не должен быть пустым или содержать пробелы.' >&2; exit 2; }
  [[ $mount_point == /* && $mount_point != *[[:space:]]* ]] || { echo 'Точка монтирования должна быть абсолютным путём без пробелов.' >&2; exit 2; }

  mkdir -p "$mount_point" /etc/davfs2
  touch /etc/davfs2/secrets /etc/fstab "$DAVFS_CONF"
  chmod 600 /etc/davfs2/secrets
  chmod 644 "$DAVFS_CONF"
  stamp=$(date +%Y%m%d_%H%M%S)
  cp -a /etc/davfs2/secrets "/etc/davfs2/secrets.before-vpn-service-$stamp"
  cp -a /etc/fstab "/etc/fstab.before-vpn-service-$stamp"
  cp -a "$DAVFS_CONF" "$DAVFS_CONF.before-vpn-service-$stamp"

  mountpoint -q "$mount_point" && umount "$mount_point"
  tune_davfs_conf "$mount_point"

  secrets_tmp=$(mktemp)
  awk -v target="$mount_point" '$1 != target' /etc/davfs2/secrets > "$secrets_tmp"
  printf '%s %s %s\n' "$mount_point" "$login" "$app_password" >> "$secrets_tmp"
  install -m 600 "$secrets_tmp" /etc/davfs2/secrets
  rm -f "$secrets_tmp"

  fstab_tmp=$(mktemp)
  awk -v target="$mount_point" '$2 != target' /etc/fstab > "$fstab_tmp"
  printf '%s %s davfs rw,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600 0 0\n' "$WEBDAV_URL" "$mount_point" >> "$fstab_tmp"
  install -m 644 "$fstab_tmp" /etc/fstab
  rm -f "$fstab_tmp"

  systemctl daemon-reload
  mount "$mount_point"
  mountpoint -q "$mount_point" || { echo 'Не удалось подключить WebDAV.' >&2; exit 1; }
  update_config "$mount_point" true
  systemctl restart vpn-service-backup.timer >/dev/null 2>&1 || true
  systemctl restart vpn-service-web.service >/dev/null 2>&1 || true

  echo 'WebDAV Яндекс.Диска подключён и выбран для резервных копий.'
  echo 'Учётные данные сохранены только в /etc/davfs2/secrets с правами 600.'
  echo 'Для этой точки монтирования параметр davfs2 delay_upload установлен в 0.'
  status
}

tune_mount() {
  local mount_point was_mounted=0
  mount_point=$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")
  if mountpoint -q "$mount_point"; then
    was_mounted=1
    umount "$mount_point"
  fi
  tune_davfs_conf "$mount_point"
  if [[ $was_mounted -eq 1 ]]; then
    mount "$mount_point"
    mountpoint -q "$mount_point" || { echo 'После изменения параметров WebDAV не удалось подключить.' >&2; exit 1; }
  fi
  echo "Для $mount_point установлен параметр delay_upload 0."
  [[ $was_mounted -eq 0 ]] || status
}

remove_mount() {
  local mount_point stamp secrets_tmp fstab_tmp
  mount_point=$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")
  stamp=$(date +%Y%m%d_%H%M%S)
  touch /etc/davfs2/secrets /etc/fstab
  cp -a /etc/davfs2/secrets "/etc/davfs2/secrets.before-vpn-service-$stamp"
  cp -a /etc/fstab "/etc/fstab.before-vpn-service-$stamp"
  mountpoint -q "$mount_point" && umount "$mount_point"
  secrets_tmp=$(mktemp)
  awk -v target="$mount_point" '$1 != target' /etc/davfs2/secrets > "$secrets_tmp"
  install -m 600 "$secrets_tmp" /etc/davfs2/secrets
  rm -f "$secrets_tmp"
  fstab_tmp=$(mktemp)
  awk -v target="$mount_point" '$2 != target' /etc/fstab > "$fstab_tmp"
  install -m 644 "$fstab_tmp" /etc/fstab
  rm -f "$fstab_tmp"
  systemctl daemon-reload
  update_config "$mount_point" false
  echo "Конфигурация WebDAV VPN Service для $mount_point удалена."
}

case "$ACTION" in
  setup) setup ;;
  status) status ;;
  mount)
    mount "$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")"
    status
    ;;
  unmount)
    umount "$(config_string YANDEX_LOCAL_PATH "$DEFAULT_MOUNT")"
    ;;
  tune) tune_mount ;;
  remove) remove_mount ;;
  *)
    echo "Использование: $0 [setup|status|mount|unmount|tune|remove]" >&2
    exit 2
    ;;
esac
