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
  TABLES=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "${DB_NAME}" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'" | tr -d '[:space:]')
  if [[ "${TABLES:-0}" != "0" ]]; then
    DB_NAME="${BASE_DB_NAME}_migration_$(date +%Y%m%d_%H%M%S)"
    i=0
    while :; do
      DB_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = $(sql_literal "$DB_NAME")" | tr -d '[:space:]')
      [[ "$DB_EXISTS" != "1" ]] && break
      i=$((i+1))
      DB_NAME="${BASE_DB_NAME}_migration_$(date +%Y%m%d_%H%M%S)_${i}"
    done
    echo "Existing PostgreSQL database ${BASE_DB_NAME} is non-empty (${TABLES} tables); using isolated migration database ${DB_NAME}."
  fi
fi

ROLE_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_roles WHERE rolname = $DB_USER_SQL" | tr -d '[:space:]')
if [[ "$ROLE_EXISTS" == "1" ]]; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "ALTER ROLE $(sql_ident "$DB_USER") WITH LOGIN PASSWORD $(sql_literal "$DB_PASSWORD");"
else
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE $(sql_ident "$DB_USER") LOGIN PASSWORD $(sql_literal "$DB_PASSWORD");"
fi

DB_FINAL_EXISTS=$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = $(sql_literal "$DB_NAME")" | tr -d '[:space:]')
if [[ "$DB_FINAL_EXISTS" != "1" ]]; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE $(sql_ident "$DB_NAME") OWNER $(sql_ident "$DB_USER");"
fi

runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION $(sql_ident "$DB_USER"); GRANT ALL ON SCHEMA public TO $(sql_ident "$DB_USER");"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "ALTER DATABASE $(sql_ident "$DB_NAME") OWNER TO $(sql_ident "$DB_USER");"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -d postgres -c "ALTER ROLE $(sql_ident "$DB_USER") IN DATABASE $(sql_ident "$DB_NAME") SET search_path = public;"

# Verify the exact TCP password that will be embedded in DATABASE_URL before
# returning control to the installer. If another process changed the role
# password, reset it once and verify again.
if ! PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -tAc 'SELECT 1' >/dev/null 2>&1; then
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "ALTER ROLE $(sql_ident "$DB_USER") WITH LOGIN PASSWORD $(sql_literal "$DB_PASSWORD");"
  PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -tAc 'SELECT 1' >/dev/null
fi

echo "PostgreSQL prepared."
echo "DATABASE_NAME=${DB_NAME}"
echo "DATABASE_USER=${DB_USER}"
echo "DATABASE_URL=postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
