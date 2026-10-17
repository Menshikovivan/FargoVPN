import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

spec = importlib.util.spec_from_file_location("postgresql_rollback", Path(__file__).resolve().parents[1] / "app/postgresql_rollback.py")
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


def test_connection_hides_password_and_preserves_ssl():
    safe, env = rollback.connection("postgresql+psycopg://user:p%40ss@127.0.0.1:5432/app?sslmode=require")
    assert safe == "postgresql://user@127.0.0.1:5432/app?sslmode=require"
    assert env["PGPASSWORD"] == "p@ss"
    assert env["PGCONNECT_TIMEOUT"] == "10"


def test_snapshot_failure_never_commits_metadata(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "config.py").write_text("DATABASE_URL = 'postgresql://user:secret@localhost/app'\n")
    def fail(*args, **kwargs):
        kwargs["stdout"].write(b"incomplete")
        raise subprocess.CalledProcessError(1, "pg_dump")
    monkeypatch.setattr(rollback.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        rollback.snapshot(app, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot.json").exists()
    assert not (tmp_path / "snapshot.sql.part").exists()


def test_restore_uses_one_transaction_and_cleans_private_script(tmp_path, monkeypatch):
    dump = tmp_path / "snapshot.sql"
    dump.write_text("CREATE SCHEMA public;\nCREATE TABLE public.old_data (id integer);\n")
    dump.chmod(0o600)
    metadata = tmp_path / "snapshot.json"
    metadata.write_text(json.dumps({"postgresql": True, "dsn": "postgresql://u:secret@localhost/db", "sha256": rollback.digest(dump)}))
    metadata.chmod(0o600)
    def check(command, **kwargs):
        assert "--single-transaction" in command
        assert "ON_ERROR_STOP=1" in command
        assert "secret" not in " ".join(command)
        content = Path(command[-1]).read_text()
        assert content.startswith("DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;")
        raise subprocess.CalledProcessError(1, "psql")
    monkeypatch.setattr(rollback.subprocess, "run", check)
    with pytest.raises(subprocess.CalledProcessError):
        rollback.restore(tmp_path / "snapshot")
    assert not list(tmp_path.glob(".rollback-*.sql"))


def test_tampered_dump_is_rejected_before_psql(tmp_path, monkeypatch):
    dump = tmp_path / "snapshot.sql"
    dump.write_text("original")
    dump.chmod(0o600)
    metadata = tmp_path / "snapshot.json"
    metadata.write_text(json.dumps({"postgresql": True, "dsn": "postgresql://u:p@localhost/db", "sha256": rollback.digest(dump)}))
    metadata.chmod(0o600)
    dump.write_text("tampered SQL")
    def forbidden(*args, **kwargs):
        raise AssertionError("psql must never run for a changed snapshot")
    monkeypatch.setattr(rollback.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="checksum mismatch"):
        rollback.restore(tmp_path / "snapshot")


def test_real_postgresql_rollback_and_failed_restore_atomicity(tmp_path):
    """Creates and drops its OWN temporary DB, never touches an existing DB."""
    admin_dsn = os.environ.get("FARGOVPN_TEST_ADMIN_DSN")
    if not admin_dsn or not shutil.which("pg_dump") or not shutil.which("psql"):
        pytest.skip("requires PostgreSQL client tools and FARGOVPN_TEST_ADMIN_DSN")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    name = "fargovpn_rollback_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'rollback-test-password'").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
        try:
            dsn = make_conninfo(admin_dsn, dbname=name, user=name, password="rollback-test-password")
            # The installer uses URLs; integration may receive libpq conninfo.
            details = psycopg.conninfo.conninfo_to_dict(dsn)
            from urllib.parse import quote
            url = "postgresql://{}:{}@{}:{}/{}".format(quote(details["user"], safe=""), quote(details.get("password", ""), safe=""), details.get("host", "127.0.0.1"), details.get("port", "5432"), name)
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE users (id integer PRIMARY KEY, value text)")
                conn.execute("INSERT INTO users VALUES (1, 'before')")
                conn.execute("CREATE VIEW original_view AS SELECT * FROM users")
                app = tmp_path / "app"
                app.mkdir()
                (app / "config.py").write_text("DATABASE_URL = " + repr(url))
                prefix = tmp_path / "snapshot"
                rollback.snapshot(app, prefix)
                conn.execute("UPDATE users SET value='after'")
                conn.execute("ALTER TABLE users ADD COLUMN new_column integer")
                conn.execute("CREATE TABLE new_table (id integer)")
                rollback.restore(prefix)
                assert conn.execute("SELECT * FROM users").fetchall() == [(1, "before")]
                assert conn.execute("SELECT * FROM original_view").fetchall() == [(1, "before")]
                assert conn.execute("SELECT to_regclass('public.new_table')").fetchone()[0] is None
                with prefix.with_suffix(".sql").open("a") as stream:
                    stream.write("\nTHIS IS INVALID SQL;\n")
                # Re-sign the intentionally invalid trusted fixture to test SQL
                # transaction atomicity separately from integrity rejection.
                metadata_path = prefix.with_suffix(".json")
                metadata = json.loads(metadata_path.read_text())
                metadata["sha256"] = rollback.digest(prefix.with_suffix(".sql"))
                metadata_path.write_text(json.dumps(metadata))
                with pytest.raises(subprocess.CalledProcessError):
                    rollback.restore(prefix)
                assert conn.execute("SELECT * FROM users").fetchall() == [(1, "before")]
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
