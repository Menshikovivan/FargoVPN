"""Run the real bootstrap decisions with an existing DB/role, without OS writes."""
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


def test_existing_database_and_role_are_not_modified(tmp_path):
    bash = os.environ.get("FARGOVPN_TEST_BASH") or shutil.which("bash")
    if not bash:
        pytest.skip("Bash required")
    source = (Path(__file__).resolve().parents[1] / "app/scripts/setup_postgresql.sh").read_text(encoding="utf-8")
    # Exclude apt/systemd/root checks, retaining the actual SQL and branch logic.
    body = source[source.index("sql_literal() {"):]
    harness = r'''set -Eeuo pipefail
DB_NAME=fargovpn
DB_USER=fargovpn
DB_HOST=127.0.0.1
DB_PORT=5432
DB_PASSWORD='p@ss:word'
runuser() {
  printf '%s\n' "$*" >> calls.log
  case "$*" in
    *"WHERE datname = 'fargovpn'"*) printf '1\n';;
    *"WHERE rolname = 'fargovpn'"*) printf '1\n';;
    *SELECT*) printf '0\n';;
  esac
}
psql() { return 0; }
''' + body
    result = subprocess.run([bash, "-c", harness], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "WITH LOGIN PASSWORD" not in calls
    assert "DROP SCHEMA" not in calls
    assert 'CREATE ROLE "fargovpn_migration_' in calls
    assert 'CREATE DATABASE "fargovpn_migration_' in calls
    assert 'ALTER DATABASE "fargovpn"' not in calls
    assert 'ALTER ROLE "fargovpn"' not in calls
    assert "DATABASE_USER=fargovpn_migration_" in result.stdout
    assert ":p%40ss%3Aword@127.0.0.1:5432/" in result.stdout


def test_real_existing_role_password_and_database_survive(tmp_path):
    admin_dsn = os.environ.get("FARGOVPN_TEST_ADMIN_DSN")
    bash = os.environ.get("FARGOVPN_TEST_BASH") or shutil.which("bash")
    if not admin_dsn or not bash or not shutil.which("psql"):
        pytest.skip("requires Bash, psql and FARGOVPN_TEST_ADMIN_DSN")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    details = conninfo_to_dict(admin_dsn)
    original = "fargovpn_boot_test_" + uuid.uuid4().hex[:10]
    new_db = new_role = None
    source = (Path(__file__).resolve().parents[1] / "app/scripts/setup_postgresql.sh").read_text()
    body = source[source.index("sql_literal() {"):]
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'original-password'").format(sql.Identifier(original)))
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(original), sql.Identifier(original)))
        try:
            old_dsn = make_conninfo(admin_dsn, dbname=original, user=original, password="original-password")
            with psycopg.connect(old_dsn, autocommit=True) as old:
                old.execute("CREATE TABLE sentinel (value text)")
                old.execute("INSERT INTO sentinel VALUES ('unchanged')")
            env = os.environ.copy()
            env.update({"PGHOST": details.get("host", "127.0.0.1"), "PGPORT": details.get("port", "5432"), "PGUSER": details["user"], "PGPASSWORD": details.get("password", ""), "PGDATABASE": details.get("dbname", "postgres"),
                        "DB_NAME": original, "DB_USER": original, "DB_HOST": details.get("host", "127.0.0.1"), "DB_PORT": details.get("port", "5432"), "DB_PASSWORD": "new-p@ss:word"})
            # Keep every actual SQL statement; only replace OS user switching.
            harness = 'set -Eeuo pipefail\nrunuser() { shift 4; command psql "$@"; }\n' + body
            result = subprocess.run([bash, "-c", harness], cwd=tmp_path, env=env, capture_output=True, text=True)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.startswith(("DATABASE_NAME=", "DATABASE_USER=", "DATABASE_URL=")))
            new_db, new_role = values.get("DATABASE_NAME"), values.get("DATABASE_USER")
            assert result.returncode == 0, result.stderr
            assert new_db != original and new_role != original
            with psycopg.connect(old_dsn) as old:
                assert old.execute("SELECT * FROM sentinel").fetchall() == [("unchanged",)]
            with psycopg.connect(values["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")) as new:
                assert new.execute("SELECT current_user").fetchone()[0] == new_role
        finally:
            prefix = original + "_migration_"
            created_databases = [row[0] for row in admin.execute("SELECT datname FROM pg_database WHERE left(datname, length(%s))=%s", (prefix, prefix))]
            created_roles = [row[0] for row in admin.execute("SELECT rolname FROM pg_roles WHERE left(rolname, length(%s))=%s", (prefix, prefix))]
            for name in set(created_databases + [new_db, original]):
                if name:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for name in set(created_roles + [new_role, original]):
                if name:
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
