#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if ! "$APP_DIR/.venv/bin/python" -c 'import config; raise SystemExit(0 if str(getattr(config, "BOT_TOKEN", "")).strip() else 1)'; then
  echo "Telegram-бот не запущен: BOT_TOKEN ещё не настроен. Укажите токен в веб-панели → Настройки → Бот и сервис." >&2
  exit 0
fi
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py"
