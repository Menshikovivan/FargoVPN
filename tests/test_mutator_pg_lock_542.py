"""Real PostgreSQL advisory lock integration; no application rows are mutated.

Set FARGOVPN_TEST_ADMIN_DSN to a disposable lab database. Business table SQL is
replaced with SELECT fixtures; advisory lock SQL, separate connections and the
actual subscription lock/context and mutation functions execute unchanged.
"""
import ast
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
import pytest

APP=Path(__file__).resolve().parents[1]/'app'

@pytest.mark.parametrize('mutation',['privacy','web_delete','bot_delete'])
def test_mutator_does_not_reacquire_family_lock_on_second_connection(mutation):
    dsn=os.environ.get('FARGOVPN_TEST_ADMIN_DSN')
    if not dsn:pytest.skip('requires explicit disposable PostgreSQL DSN')
    psycopg=pytest.importorskip('psycopg')
    commands=[]
    class Conn:
        def __enter__(self):
            self.c=psycopg.connect(dsn)
            self.c.execute("SET lock_timeout='750ms'")
            return self
        def __exit__(self,exc,*args):
            try:self.c.rollback() if exc else self.c.commit()
            finally:self.c.close()
        def execute(self,sql,args=()):
            commands.append(sql)
            if sql.startswith('SELECT COUNT(*)'):return self.c.execute('SELECT 0')
            if sql.startswith('DELETE FROM users'):return self.c.execute('SELECT 1')
            return self.c.execute(sql.replace('?','%s'),args)
    env={'contextmanager':contextmanager,'Path':Path,'_connect':lambda *a:Conn(),'logger':logging.getLogger(__name__)}
    tree=ast.parse((APP/'services/subscriptions.py').read_text())
    lock=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_subscription_identity_lock_sync')
    exec(compile(ast.Module(body=[lock],type_ignores=[]),'actual-subscription-lock','exec'),env)
    row={'tg_id':54297531,'username':'synthetic','email':'','uuid':'','sub_id':''}
    class HTTPException(Exception):pass
    env.update({'subscriptions':NS(_subscription_identity_lock_sync=env['_subscription_identity_lock_sync']),
        'privacy':NS(erase_user_data=lambda *a,**kw:{'limits':'synthetic'}),'config':NS(DB_PATH='unused'),
        'database':Conn,'database_adapter':NS(connect=lambda *a,**kw:Conn()),'get_user':lambda _:row,'db_get_user':lambda _:row,
        'fetch_snapshot_sync':lambda **kw:{'clients':[],'error':'','stale':False},'delete_client_sync':lambda *a,**kw:None,
        'HTTPException':HTTPException,'Request':object,'audit':lambda *a:None,'user_events':NS(safe_record_event=lambda *a,**kw:None),
        'set_flash':lambda *a:None,'RedirectResponse':lambda *a:a,'public_path':lambda p:p,'require_auth':lambda *a:None})
    for file,names in [('webapp.py',{'_privacy_family_guard','_privacy_remote_matches','_erase_profile_confirmed','delete_user'}),('main.py',{'_delete_crm_user_sync'})]:
        nodes=[n for n in ast.parse((APP/file).read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
        for n in nodes:n.decorator_list=[]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP/file),'exec'),env)
    if mutation=='privacy':env['_erase_profile_confirmed'](row['tg_id'],row['tg_id'],'test-admin')
    elif mutation=='web_delete':env['delete_user'](NS(session={'user':'test-admin'}),row['tg_id'])
    else:assert env['_delete_crm_user_sync'](row['tg_id'],'test-admin') is True
    assert any('pg_advisory_lock(495' in sql for sql in commands)
    assert not any('pg_advisory_xact_lock(495' in sql for sql in commands)
    with psycopg.connect(dsn) as check:
        assert check.execute('SELECT pg_try_advisory_lock(495, hashtext(%s))',(str(row['tg_id']),)).fetchone()[0]
        check.execute('SELECT pg_advisory_unlock(495, hashtext(%s))',(str(row['tg_id']),))
