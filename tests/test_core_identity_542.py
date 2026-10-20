"""Actual-function regressions with synthetic storage; no external requests."""
import ast
import logging
import sqlite3
import traceback
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import pytest

APP=Path(__file__).resolve().parents[1]/'app'
def node(file,name):
    return next(n for n in ast.parse((APP/file).read_text(encoding='utf-8-sig')).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
def function(file,name,env):
    n=node(file,name);n.decorator_list=[]
    exec(compile(ast.Module(body=[n],type_ignores=[]),str(APP/file),'exec'),env)
    return env[name]

def test_release_cleanup_failure_does_not_abort_success():
    tree=ast.parse((APP/'update_manager.py').read_text(encoding='utf-8-sig'))
    assignments=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='LOGGER' for t in n.targets)]
    def failing_delete(_): raise OSError('synthetic cleanup failure')
    checked=0
    for n in ast.walk(tree):
        if not isinstance(n,ast.Try): continue
        if not any(isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute) and isinstance(x.func.value,ast.Name) and x.func.value.id=='LOGGER' for h in n.handlers for expr in h.body for x in ([expr.value] if isinstance(expr,ast.Expr) else [])): continue
        if not any(isinstance(x,ast.Call) and isinstance(x.func,ast.Name) and x.func.id=='_github_delete_ref' for x in ast.walk(n)): continue
        env=dict(logging=logging,__name__='synthetic',_github_delete_ref=failing_delete,backup_ref='synthetic',main_sync={'backup_ref':'synthetic'})
        exec(compile(ast.Module(body=assignments+[n],type_ignores=[]),'cleanup','exec'),env);checked+=1
    assert checked==2

@pytest.mark.parametrize('success',[True,False])
def test_broadcast_records_each_success_and_terminal_failure(tmp_path,success):
    statuses=[];sent=[];journals=[]
    manager=types.SimpleNamespace(read_manifest=lambda j:{'job_id':j,'kind':'text','message':'synthetic','recipients':[{'tg_id':910001},{'tg_id':910002}]},write_status=lambda *a,**kw:statuses.append({'state':a[1],**kw}),log_path=lambda j:tmp_path/'broadcast.log',read_status=lambda j:{'progress':3})
    class Client:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
    payload={'result':{'message_id':123}} if success else {'_http_status':403}
    env={'Any':object,'Path':Path,'datetime':datetime,'timezone':timezone,'os':__import__('os'),'time':types.SimpleNamespace(sleep=lambda _:None),'traceback':traceback,'broadcast_manager':manager,'httpx':types.SimpleNamespace(Client=Client,Timeout=lambda *a,**kw:None),'config':types.SimpleNamespace(),'message_journal':types.SimpleNamespace(record_outgoing_sync=lambda *a,**kw:journals.append(kw)),'_telegram_call':lambda c,m,**kw:(sent.append(kw['data']['chat_id']) or (success,payload,'synthetic')),'_set_telegram_delivery_state':lambda *a,**kw:None,'_journal_text':lambda m:m['message'],'_journal_message_id':lambda p:123,'_record_result':lambda *a:None,'_is_terminal_recipient_error':lambda *a:True}
    assert function('broadcast_worker.py','run',env)('synthetic')==0
    assert sent==[910001,910002]
    assert len(journals)==2
    assert statuses[-1]['state']=='completed'

@pytest.mark.parametrize('trusted,expected',[({123456:'verified_handle'},'verified_handle'),({123456:''},None),({},None)])
def test_identity_normalization_is_idempotent(trusted,expected):
    identity={'__name__':'offline_identity'}
    exec(compile((APP/'user_identity.py').read_text(encoding='utf-8-sig'),'identity','exec'),identity)
    run=node('normalize_user_identities.py','run')
    loop=next(n for n in ast.walk(run) if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='row')
    block=next(n for n in loop.body if isinstance(n,ast.If) and ast.unparse(n.test)=="'username' in user_columns")
    value='verified_handle'
    for _ in range(3):
        env=dict(user_columns={'username'},values={'username':value,'identity_source':'3x-ui:manual-subscription-bind'},tg_id=123456,changes={},trusted_usernames=trusted,counts=dict.fromkeys(['telegram_usernames_recovered','untrusted_panel_usernames_cleared','historical_usernames_ignored','usernames_normalized'],0),normalize_username=identity['normalize_username'])
        exec(compile(ast.Module(body=[block],type_ignores=[]),'normalization','exec'),env)
        value=env['changes'].get('username',value)
        assert value==expected

@pytest.mark.parametrize('incoming,expected',[(None,'verified_handle'),('',None),('new_handle','new_handle')])
def test_absent_xui_recovery_distinguishes_missing_update_and_removed_username(tmp_path,incoming,expected):
    dbpath=tmp_path/'synthetic.db'
    @contextmanager
    def connect(_path=None):
        c=sqlite3.connect(dbpath)
        try:
            with c:yield c
        finally:c.close()
    with connect() as c:
        c.execute('CREATE TABLE users(tg_id INTEGER PRIMARY KEY,username TEXT,email TEXT)')
        c.execute('INSERT INTO users VALUES(101,?,?)',('verified_handle',''))
    def get_user(tg_id,_path=None):
        with connect() as c:
            c.row_factory=sqlite3.Row
            row=c.execute('SELECT * FROM users WHERE tg_id=?',(tg_id,)).fetchone()
            return dict(row) if row else None
    env={'Path':Path,'Any':object,'normalize_username':lambda v,_id:str(v or '').lstrip('@'),'get_user_by_tg_id':get_user,'_client_from_snapshot_by_email':lambda *a,**k:None,'find_client_by_telegram_id_sync':lambda *a,**k:None,'_connect':connect}
    function('services/subscriptions.py','recover_user_identity_sync',env)(101,incoming,str(dbpath),False)
    assert get_user(101)['username']==expected

