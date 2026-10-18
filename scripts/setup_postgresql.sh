#!/usr/bin/env bash
set -euo pipefail
STEP=0
step(){ STEP=$((STEP+1)); echo "[PostgreSQL] Шаг ${STEP}: $*"; }
run_timed(){ local sec="$1" label="$2"; shift 2; echo "  Ожидается: ${label} (таймаут ${sec} с)"; timeout --kill-after=5s "${sec}s" "$@"; }

DB_NAME="${FARGOVPN_DB_NAME:-fargovpn}"
DB_USER="${FARGOVPN_DB_USER:-fargovpn}"
DB_HOST="${FARGOVPN_DB_HOST:-127.0.0.1}"
DB_PORT="${FARGOVPN_DB_PORT:-5432}"
DB_PASSWORD="${FARGOVPN_DB_PASSWORD:-}"

[[ $EUID -eq 0 ]] || { echo "Запустите от root"; exit 1; }
[[ "$DB_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Некорректное имя PostgreSQL БД: $DB_NAME" >&2; exit 2; }
[[ "$DB_USER" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Некорректное имя PostgreSQL пользователя: $DB_USER" >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive
step "Проверка системных зависимостей"
run_timed 180 "apt update" apt-get update
run_timed 300 "установка PostgreSQL" apt-get install -y --no-install-recommends postgresql postgresql-client openssl
step "Запуск PostgreSQL"
run_timed 45 "enable/start PostgreSQL" systemctl enable --now postgresql

if [[ -z "$DB_PASSWORD" ]]; then
  DB_PASSWORD="$(openssl rand -hex 24)"
fi

sql_literal() {
  local v="$1"
  printf "'%s'" "${v//\'/\'\'}"
}
sql_ident() {
  local v="$1"
  printf '"%s"' "${v//\"/\"\"}"
}

BASE_DB_NAME="$DB_NAME"
DB_NAME_SQL=$(sql_literal "$DB_NAME")
DB_USER_SQL=$(sql_literal "$DB_USER")
DB_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = $DB_NAME_SQL" | tr -d '[:space:]')

if [[ "$DB_EXISTS" == "1" ]]; then
  # Never clear an existing schema, even one containing only views/functions.
  DB_NAME="${BASE_DB_NAME:0:32}_migration_$(date +%Y%m%d_%H%M%S)"
  i=0
  while :; do
    DB_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = $(sql_literal "$DB_NAME")" | tr -d '[:space:]')
    [[ "$DB_EXISTS" != "1" ]] && break
    i=$((i+1))
    DB_NAME="${BASE_DB_NAME:0:32}_migration_$(date +%Y%m%d_%H%M%S)_${i}"
  done
  echo "Existing PostgreSQL database retained; using isolated database ${DB_NAME}."
fi
ROLE_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_roles WHERE rolname = $DB_USER_SQL" | tr -d '[:space:]')
if [[ "$ROLE_EXISTS" == "1" ]]; then
  # A role can serve other databases/installations. Never rotate its password.
  BASE_DB_USER="$DB_USER"
  DB_USER="${BASE_DB_USER:0:32}_migration_$(date +%Y%m%d_%H%M%S)"
  i=0
  while :; do
    ROLE_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_roles WHERE rolname = $(sql_literal "$DB_USER")" | tr -d '[:space:]')
    [[ "$ROLE_EXISTS" != "1" ]] && break
    i=$((i+1))
    DB_USER="${BASE_DB_USER:0:32}_migration_$(date +%Y%m%d_%H%M%S)_${i}"
  done
fi
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE $(sql_ident "$DB_USER") LOGIN PASSWORD $(sql_literal "$DB_PASSWORD");"
DB_FINAL_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = $(sql_literal "$DB_NAME")" | tr -d '[:space:]')
if [[ "$DB_FINAL_EXISTS" != "1" ]]; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE $(sql_ident "$DB_NAME") OWNER $(sql_ident "$DB_USER");"
else
  echo 'Database name was claimed during setup; refusing to alter it. Retry setup.' >&2
  exit 1
fi

runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -c "CREATE SCHEMA IF NOT EXISTS public AUTHORIZATION $(sql_ident "$DB_USER"); GRANT ALL ON SCHEMA public TO $(sql_ident "$DB_USER");"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "ALTER DATABASE $(sql_ident "$DB_NAME") OWNER TO $(sql_ident "$DB_USER");"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "ALTER ROLE $(sql_ident "$DB_USER") IN DATABASE $(sql_ident "$DB_NAME") SET search_path = public;"

# A failed connection must never trigger a password change on a shared role.
PGPASSWORD="$DB_PASSWORD" PGCONNECT_TIMEOUT=10 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -tAc 'SELECT 1' >/dev/null
echo "PostgreSQL prepared."
echo "DATABASE_NAME=${DB_NAME}"
echo "DATABASE_USER=${DB_USER}"
ENCODED_PASSWORD=$(PGPASSWORD="$DB_PASSWORD" python3 -c 'import os; from urllib.parse import quote; print(quote(os.environ["PGPASSWORD"], safe=""))')
echo "DATABASE_URL=postgresql+psycopg://${DB_USER}:${ENCODED_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
