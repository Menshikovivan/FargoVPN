from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest

APP = Path(__file__).resolve().parents[1] / 'app'

def functions():
    tree = ast.parse((APP / 'restore_manager.py').read_text(encoding='utf-8-sig'))
    ns = {'Any': object, 'subprocess': None}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('USERS_ONLY_') for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'constants', 'exec'), ns)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {'_users_only_order', '_restore_service_states'}:
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'functions', 'exec'), ns)
    return ns

def catalog(rows):
    return SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: rows))

def test_family_fk_closure_is_parent_first():
    ns = functions()
    rows = [('telegram_profile_links', 'users', 'public', 'public'), ('telegram_profile_link_requests', 'users', 'public', 'public')]
    order = ns['_users_only_order'](catalog(rows))
    assert order.index('users') < order.index('telegram_profile_links')
    assert 'telegram_profile_link_requests' not in ns['USERS_ONLY_TABLES']

@pytest.mark.parametrize('row', [('unknown', 'users', 'public', 'public'), ('users','users','other','public'), ('users','users','public','public')])
def test_unknown_cross_schema_and_cyclic_fks_fail_closed(row):
    with pytest.raises(RuntimeError):
        functions()['_users_only_order'](catalog([row]))

def test_rollback_restores_stopped_enabled_unit_without_starting():
    ns = functions()
    calls = []
    active = {'on.service': True, 'off.service': False}
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'is-active':
            return SimpleNamespace(returncode=0 if active[args[-1]] else 3)
        if args[1] in {'start','stop'}:
            active[args[-1]] = args[1] == 'start'
        return SimpleNamespace(returncode=0)
    ns['subprocess'] = SimpleNamespace(run=run)
    ns['_restore_service_states']({'on.service': {'active':True,'enabled':False},'off.service': {'active':False,'enabled':True}})
    assert ['systemctl','enable','off.service'] in calls
    assert ['systemctl','stop','off.service'] in calls
    assert ['systemctl','start','off.service'] not in calls

def test_users_only_staging_skips_archived_security_data_before_column_validation(monkeypatch):
    import sys
    tree = ast.parse((APP / 'restore_manager.py').read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'restore_dump')
    class Context:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): return None
        def cursor(self): return self
    consumed = []
    def rows():
        consumed.append(True)
        yield b'not imported\n'
    calls = []
    def forbidden(*args): raise AssertionError('Excluded archive security table must not be inspected/imported')
    ns = {'Path':Path, 'raw_dsn':lambda x:x,
          '_clone_live_schema':lambda *args,**kwargs:calls.append(kwargs.get('section','pre-data')),
          '_copy_data_blocks':lambda dump: [('webauthn_credentials',['unknown_column'],rows())],
          '_table_columns':forbidden}
    monkeypatch.setitem(sys.modules,'psycopg',SimpleNamespace(connect=lambda dsn:Context(),sql=SimpleNamespace()))
    exec(compile(ast.Module(body=[node],type_ignores=[]),'restore_dump','exec'),ns)
    ns['restore_dump'](Path('synthetic.sql'),'stage',schema_dsn='live',include_tables=('users',))
    assert consumed == [True]
    assert calls == ['pre-data','post-data']
