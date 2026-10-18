"""Offline security regressions; execute only selected pure functions via AST."""
import ast
import hashlib
import hmac
import json
import time
import types
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'app'

def selected(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return namespace

class HTTPException(Exception):
    def __init__(self, status_code, detail=''):
        self.status_code = status_code

def auth_context():
    cfg = types.SimpleNamespace(WEB_USERNAME='admin', WEB_PASSWORD_HASH='original', WEB_SESSION_MAX_AGE_SECONDS=28800)
    ns = selected('webapp.py', {'auth_credentials_version', '_secure_text_compare', 'require_auth'}, dict(config=cfg, hashlib=hashlib, hmac=hmac, json=json, time=time, Request=object, HTTPException=HTTPException))
    return cfg, ns

def request(version=None, method='GET'):
    session = dict(auth=True, user='admin', login_at=int(time.time()), csrf_token='csrf')
    if version is not None:
        session['auth_version'] = version
    return types.SimpleNamespace(session=session, method=method, headers={'x-csrf-token': 'csrf'})

def rejected(ns, req, status):
    try:
        ns['require_auth'](req)
    except HTTPException as e:
        assert e.status_code == status
    else:
        raise AssertionError('Request unexpectedly authorized')

def test_credentials_revocation_and_csrf():
    cfg, ns = auth_context()
    version = ns['auth_credentials_version']()
    ns['require_auth'](request(version))
    ns['require_auth'](request(version, 'POST'))
    legacy = request()
    rejected(ns, legacy, 401)
    assert not legacy.session
    cfg.WEB_PASSWORD_HASH = 'changed'
    stolen = request(version)
    rejected(ns, stolen, 401)
    assert not stolen.session
    version2 = ns['auth_credentials_version']()
    ns['require_auth'](request(version2))
    cfg.WEB_USERNAME = 'new-admin'
    rejected(ns, request(version2), 401)
    current = request(ns['auth_credentials_version'](), 'POST')
    current.headers = {}
    rejected(ns, current, 403)

def test_archive_limits():
    limits_ns = selected('restore_manager.py', {'_archive_limits'}, dict(_cfg=lambda name, default: default, ARCHIVE_MAX_MEMBERS=50000, ARCHIVE_MAX_UNPACKED_BYTES=4*1024**3, ARCHIVE_MAX_FILE_BYTES=512*1024**2))
    assert limits_ns['_archive_limits']() == (50000, 4*1024**3, 512*1024**2)
    limits_ns['_cfg'] = lambda name, default: {'RESTORE_ARCHIVE_MAX_UNPACKED_MB': 16, 'RESTORE_ARCHIVE_MAX_FILE_MB': 2}.get(name, default)
    assert limits_ns['_archive_limits']() == (50000, 16*1024**2, 2*1024**2)
    limits_ns['_cfg'] = lambda name, default: -1
    assert limits_ns['_archive_limits']() == (100, 1024**2, 1024**2)


def copy_context():
    import re
    return selected('restore_manager.py', {'_copy_header', '_copy_data_blocks'}, dict(re=re, Path=Path))


def test_untrusted_dump_only_yields_copy_data(tmp_path):
    ns = copy_context()
    dump = tmp_path / 'untrusted.sql'
    # SQL/function/psql instructions are never returned as executable statements.
    dump.write_bytes(b'\\! touch /tmp/SHOULD_NOT_EXIST\nCREATE FUNCTION evil() RETURNS void AS $$bad$$ LANGUAGE c;\nCOPY "public"."users" (\n "tg_id", "name,quoted", "null_value"\n) FROM stdin;\n1\ttext\\\\.literal\t\\N\n2\t\\\\.\tNULL\n\\.\nSELECT pg_catalog.setval(\'evil\', 900);\n')
    blocks = []
    for table, columns, rows in ns['_copy_data_blocks'](dump):
        blocks.append((table, columns, list(rows)))
    assert blocks == [('users', ['tg_id', 'name,quoted', 'null_value'], [b'1\ttext\\\\.literal\t\\N\n', b'2\t\\\\.\tNULL\n'])]


def test_copy_parser_rejects_non_data_grammar(tmp_path):
    import pytest
    ns = copy_context()
    for header in ('COPY public.users FROM PROGRAM \'id\';',
                   'COPY private.users (id) FROM stdin;',
                   'COPY public.users (id, id) FROM stdin;',
                   'COPY public.users (id,) FROM stdin;',
                   'COPY public.users (id) FROM stdin; DROP TABLE users;'):
        with pytest.raises(RuntimeError):
            ns['_copy_header'](header)
    assert ns['_copy_header']('COPY "public"."odd""table" ("a""b", plain) FROM stdin;') == ('odd"table', ['a"b', 'plain'])
    for payload in (b'COPY public.users (id) FROM stdin;\n1\n',
                    b'COPY public.users (id) FROM stdin;\n\\.\nCOPY public.users (id) FROM stdin;\n\\.\n',
                    b'COPY public.users (id) FROM stdin;\n' + b'x' * (16*1024*1024+1),
                    b'COPY public.users (' + b'x' * 65536):
        dump = tmp_path / 'invalid.sql'
        dump.write_bytes(payload)
        with pytest.raises(RuntimeError):
            for _, _, rows in ns['_copy_data_blocks'](dump):
                list(rows)


def test_archive_units_and_active_configs_are_never_applied(tmp_path):
    import logging
    ns = selected('restore_manager.py', {'_restore_systemd_units', '_restore_system_configs'}, dict(Path=Path, logger=logging.getLogger('security-test')))
    backup = tmp_path / 'archive'
    backup.mkdir()
    ns['_restore_systemd_units'](backup)
    ns['_restore_system_configs'](backup, xui_target=tmp_path / 'installed')
    assert not (tmp_path / 'installed').exists()


def test_sqlite_xui_import_preserves_settings_and_ignores_archive_triggers(tmp_path):
    import os
    import sqlite3
    ns = selected('restore_manager.py', {'_sqlite_xui_swap', '_quoted_columns'}, dict(Path=Path, sqlite3=sqlite3, os=os, closing=closing))
    def snapshot(source, destination):
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
    ns.update(_sqlite_integrity=lambda path: 'ok', _sqlite_snapshot=snapshot, _sqlite_remove_sidecars=lambda path: None)
    archive, live = tmp_path / 'archive.db', tmp_path / 'live.db'
    for path, value in ((archive, 'untrusted'), (live, 'trusted')):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript('CREATE TABLE settings(value TEXT); CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);')
            conn.execute('INSERT INTO settings VALUES (?)', (value,))
            conn.execute('INSERT INTO users VALUES (1, ?)', (value,))
    with closing(sqlite3.connect(archive)) as conn, conn:
        conn.execute("CREATE TRIGGER malicious AFTER INSERT ON users BEGIN UPDATE settings SET value='archive-code'; END")
    ns['_sqlite_xui_swap'](tmp_path, archive, live, 'test')
    with closing(sqlite3.connect(live)) as conn:
        assert conn.execute('SELECT value FROM settings').fetchall() == [('trusted',)]
        assert conn.execute('SELECT name FROM users').fetchall() == [('untrusted',)]
        assert not conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()


def test_literal_config_restore_uses_business_allowlist(tmp_path):
    import os
    import re
    ns = selected('restore_manager.py', {'merge_config', 'parse_literal_config'}, dict(ast=ast, Path=Path, re=re, os=os, Any=object,
        RESTORABLE_CONFIG={'SERVICE_NAME', 'PAYMENT_PRICE'}, PROTECTED_CONFIG=set()))
    installed, archive = tmp_path / 'installed.py', tmp_path / 'archive.py'
    installed.write_text('SERVICE_NAME = "Live"\nWEB_SECRET_KEY = "live-secret"\n', encoding='utf-8')
    archive.write_text('SERVICE_NAME = "Backup\\nName"\nWEB_SECRET_KEY = "attacker"\nUPDATE_DIR = "/etc"\nimport os\nos.system("touch SHOULD_NOT_EXIST")\n', encoding='utf-8')
    ns['merge_config'](installed, archive)
    values = ns['parse_literal_config'](installed)
    assert values['SERVICE_NAME'] == 'Backup\nName'
    assert values['WEB_SECRET_KEY'] == 'live-secret'
    assert 'UPDATE_DIR' not in values
    assert 'os.system' not in installed.read_text(encoding='utf-8')


def test_sqlite_xui_restore_rejects_broken_foreign_keys_without_live_changes(tmp_path):
    import os
    import sqlite3
    import pytest
    ns = selected('restore_manager.py', {'_sqlite_xui_swap', '_quoted_columns'}, dict(Path=Path, sqlite3=sqlite3, os=os, closing=closing))
    def snapshot(source, destination):
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
    ns.update(_sqlite_integrity=lambda path: 'ok', _sqlite_snapshot=snapshot, _sqlite_remove_sidecars=lambda path: None)
    archive, live = tmp_path / 'archive.db', tmp_path / 'live.db'
    for path, parent_id in ((archive, 999), (live, 1)):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript('CREATE TABLE parents(id INTEGER PRIMARY KEY); CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents(id));')
            conn.execute('INSERT INTO parents VALUES (1)')
            conn.execute('INSERT INTO children VALUES (1, ?)', (parent_id,))
    with pytest.raises(RuntimeError, match='внешние ключи'):
        ns['_sqlite_xui_swap'](tmp_path, archive, live, 'fk-test')
    with closing(sqlite3.connect(live)) as conn:
        assert conn.execute('SELECT parent_id FROM children').fetchall() == [(1,)]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not list(tmp_path.glob('*.restore_tmp_*'))
