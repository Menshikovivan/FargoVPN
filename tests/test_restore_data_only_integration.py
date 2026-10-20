"""Real PostgreSQL restore tests. Requires explicitly supplied disposable admin DSN."""
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest


def test_real_postgres_copy_restore_and_no_code_execution(tmp_path):
    admin_dsn = os.environ.get('FARGOVPN_RESTORE_TEST_ADMIN_DSN')
    if not admin_dsn:
        pytest.skip('Disposable Linux PostgreSQL test server is not configured')
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    root = Path(__file__).resolve().parents[1] / 'app'
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('restore_integration', root / 'restore_manager.py')
    manager = importlib.util.module_from_spec(spec)
    with patch('logging.FileHandler', return_value=logging.NullHandler()):
        spec.loader.exec_module(manager)
    suffix = uuid.uuid4().hex[:10]
    role, source_name = 'restore_test_' + suffix, 'restore_source_' + suffix
    password = uuid.uuid4().hex
    databases = [source_name]
    admin = psycopg.connect(admin_dsn, autocommit=True)
    sentinel = tmp_path / 'EXECUTED_ARCHIVE_CODE'
    def dsn(database):
        return make_url(admin_dsn).set(username=role, password=password, database=database).render_as_string(hide_password=False)
    def stage():
        name = 'restore_stage_' + uuid.uuid4().hex[:10]
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name), sql.Identifier(role)))
        databases.append(name)
        return dsn(name)
    try:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS').format(sql.Identifier(role), sql.Literal(password)))
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(source_name), sql.Identifier(role)))
        source_dsn = dsn(source_name)
        with psycopg.connect(source_dsn) as conn:
            conn.execute('CREATE TABLE users(id BIGSERIAL PRIMARY KEY, name TEXT, nullable TEXT)')
            conn.execute('CREATE TABLE payments(id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), note TEXT)')
            conn.execute('CREATE TABLE referral_rewards(id BIGSERIAL PRIMARY KEY, value TEXT)')
            conn.execute('CREATE TABLE telegram_profile_links(telegram_id BIGINT PRIMARY KEY, profile_tg_id BIGINT REFERENCES users(id))')
            for table in manager.USERS_ONLY_REVOKE_TABLES:
                conn.execute(sql.SQL('CREATE TABLE {} (id BIGINT PRIMARY KEY)').format(sql.Identifier(table)))
            conn.execute("INSERT INTO referral_rewards(id,value) VALUES (7,'reward')")
            conn.execute('INSERT INTO users(id,name,nullable) VALUES (%s,%s,%s)', (41, 'Р®РЅРёРєРѕРґ\\.\nline\tTAB \\N', None))
            conn.execute('INSERT INTO payments(id,user_id,note) VALUES (4,41,%s)', ('payment text',))
        dump = tmp_path / 'archive.sql'
        cli_dsn, env = manager.dsn_password_and_cli(source_dsn)
        subprocess.run(['pg_dump', '--no-owner', '--no-privileges', '--dbname', cli_dsn, '--file', str(dump)], env=env, check=True)
        original = dump.read_bytes()
        payload = ('\\! touch ' + str(sentinel) + '\nDROP TABLE public.users CASCADE;\nSELECT pg_write_file(\'' + str(sentinel) + "', 'bad');\n").encode()
        dump.write_bytes(payload + original + payload)
        restored = stage()
        manager.restore_dump(dump, restored, schema_dsn=source_dsn)
        assert not sentinel.exists()
        with psycopg.connect(restored) as conn:
            assert conn.execute('SELECT id,name,nullable FROM users').fetchall() == [(41, 'Р®РЅРёРєРѕРґ\\.\nline\tTAB \\N', None)]
            assert conn.execute('SELECT user_id,note FROM payments').fetchall() == [(41, 'payment text')]
            assert conn.execute("INSERT INTO users(name) VALUES ('next') RETURNING id").fetchone()[0] == 42
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute("INSERT INTO payments(user_id) VALUES (999)")
        unknown = tmp_path / 'unknown.sql'
        unknown.write_text('COPY public.unknown (id) FROM stdin;\n1\n\\.\n', encoding='utf-8')
        with pytest.raises(RuntimeError, match='несовместимы'):
            manager.restore_dump(unknown, stage(), schema_dsn=source_dsn)
        wrong_fk = tmp_path / 'wrong-fk.sql'
        wrong_fk.write_text('COPY public.users (id,name) FROM stdin;\n1\tvalid\n\\.\nCOPY public.payments (id,user_id) FROM stdin;\n1\t999\n\\.\n', encoding='utf-8')
        failed = stage()
        with pytest.raises(RuntimeError, match="foreign key constraint"):
            manager.restore_dump(wrong_fk, failed, schema_dsn=source_dsn)
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 1
        # The supported users/payments/family closure restores with its FKs intact.
        first_result = manager._selected_restore_with_snapshot(source_dsn, restored, {})
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute('SELECT user_id,note FROM payments').fetchall() == [(41, 'payment text')]
            assert conn.execute("SELECT COUNT(*) FROM pg_constraint WHERE conname='payments_user_id_fkey'").fetchone()[0] == 1
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(first_result['snapshot_schema'])))
            conn.execute('CREATE TABLE external_user_reference(user_id BIGINT REFERENCES users(id))')
        # An unsupported table referencing users must still refuse before changes.
        with pytest.raises(RuntimeError, match='неподдерживаемая внешняя зависимость'):
            manager._selected_restore_with_snapshot(source_dsn, restored, {})
        with psycopg.connect(source_dsn) as conn:
            conn.execute('DROP TABLE external_user_reference')
            conn.execute('CREATE TABLE audit_log(message TEXT)')
            conn.execute("INSERT INTO audit_log VALUES ('live journal must survive')")
            conn.execute("UPDATE users SET name='live old data'")
        result = manager._selected_restore_with_snapshot(source_dsn, restored, {'users': 1, 'payments': 1, 'referral_rewards': 1})
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute('SELECT name FROM users').fetchone()[0] == 'Р®РЅРёРєРѕРґ\\.\nline\tTAB \\N'
            assert conn.execute('SELECT message FROM audit_log').fetchall() == [('live journal must survive',)]
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(result['snapshot_schema'])))
        with pytest.raises(RuntimeError, match='ожидалось'):
            manager._selected_restore_with_snapshot(source_dsn, restored, {'users': 999})
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 1
            assert conn.execute('SELECT message FROM audit_log').fetchall() == [('live journal must survive',)]
        with pytest.raises(RuntimeError, match='SUPERUSER'):
            manager._clone_live_schema(admin_dsn, stage())
    finally:
        for database in reversed(databases):
            admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
        admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        admin.close()
