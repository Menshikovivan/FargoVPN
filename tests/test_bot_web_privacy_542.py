"""Offline tests of real handlers with isolated synthetic boundaries."""
import ast
import asyncio
import html
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest

APP=Path(__file__).resolve().parents[1]/'app'
def immediate(coro):
    try: coro.send(None)
    except StopIteration as done:return done.value
    finally:coro.close()
    raise AssertionError('unexpected asynchronous boundary')
def load(file,names,env):
    tree=ast.parse((APP/file).read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes)==len(names)
    for n in nodes:n.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP/file),'exec'),env)
    return env
class HTTPException(Exception):
    def __init__(self,status,detail=''):self.status_code=status;self.detail=detail

def privacy_env(*,family=0,shared=0,snapshots=None,erase_error=False):
    calls=[]
    row={'tg_id':123,'username':'synthetic','uuid':'uuid-test','email':'email-test','sub_id':'sub-test'}
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args):
            calls.append(('sql',sql,args));self.value=family if 'telegram_profile_links' in sql else shared;return self
        def fetchone(self):return (self.value,)
    values=iter(snapshots or [{'clients':[dict(row)],'error':'','stale':False},{'clients':[],'error':'','stale':False}])
    def snapshot(**kw):
        assert kw=={'force':True};calls.append(('snapshot',));return next(values)
    def erase(*a,**kw):
        calls.append(('erase',a,kw))
        if erase_error:raise RuntimeError('synthetic database failure')
        return {'limits':'external copies retained'}
    env={'subscriptions':NS(_subscription_identity_lock_sync=lambda *a:nullcontext()),'privacy':NS(erase_user_data=erase),'config':NS(DB_PATH='unused'),'database':Conn,'get_user':lambda _:row,'fetch_snapshot_sync':snapshot,'delete_client_sync':lambda *a,**kw:calls.append(('delete',a,kw)),'HTTPException':HTTPException}
    load('webapp.py',{'_privacy_family_guard','_privacy_remote_matches','_erase_profile_confirmed'},env)
    return env,calls,row

def test_privacy_erasure_preserves_full_snapshot_until_atomic_local_delete():
    env,calls,row=privacy_env()
    assert env['_erase_profile_confirmed'](123,123,'admin')['limits']
    erasure=next(c for c in calls if c[0]=='erase')
    assert erasure[2]['identity_snapshot']==row
    assert erasure[2]['allow_current_credentials'] is True and erasure[2]['vpn_revoked'] is True
    assert [c[0] for c in calls if c[0] in {'snapshot','delete','erase'}]==['snapshot','delete','snapshot','erase']
    assert not any(c[0]=='sql' and c[1].startswith(('DELETE','UPDATE')) for c in calls)

@pytest.mark.parametrize('family,shared',[(1,0),(0,1)])
def test_privacy_family_and_shared_profile_refuse_before_remote_mutation(family,shared):
    env,calls,_=privacy_env(family=family,shared=shared)
    with pytest.raises(HTTPException) as error:env['_erase_profile_confirmed'](123,123,'admin')
    assert error.value.status_code==409
    assert not any(c[0] in {'snapshot','delete','erase'} for c in calls)

@pytest.mark.parametrize('bad_snapshot',[{'clients':[],'stale':True},{'clients':[],'error':'network failure'},{},None])
def test_privacy_network_failure_never_means_absent_client(bad_snapshot):
    env,calls,_=privacy_env(snapshots=[bad_snapshot])
    with pytest.raises(HTTPException) as error:env['_erase_profile_confirmed'](123,123,'admin')
    assert error.value.status_code==502
    assert not any(c[0]=='erase' for c in calls)

def test_privacy_partial_remote_success_local_failure_is_explicit_and_retryable():
    env,calls,_=privacy_env(erase_error=True)
    with pytest.raises(HTTPException) as error:env['_erase_profile_confirmed'](123,123,'admin')
    assert 'уже отозван' in error.value.detail and 'повторите' in error.value.detail
    env,calls,_=privacy_env(snapshots=[{'clients':[]},{'clients':[]}])
    env['_erase_profile_confirmed'](123,123,'admin')
    assert not any(c[0]=='delete' for c in calls)
    assert any(c[0]=='erase' for c in calls)

def test_privacy_confirmation_mismatch_never_reaches_dependencies():
    env,calls,_=privacy_env()
    with pytest.raises(HTTPException):env['_erase_profile_confirmed'](123,124,'admin')
    assert calls==[]

def test_privacy_post_authentication_and_csrf_precede_erasure():
    calls=[]
    def reject(*args):calls.append(args);raise HTTPException(403)
    env={'Request':object,'Form':lambda x:x,'require_auth':reject,'_erase_profile_confirmed':lambda *a:pytest.fail('must not erase unauthenticated'),'set_flash':lambda *a:None,'RedirectResponse':lambda *a:a,'public_path':lambda p:p}
    load('webapp.py',{'privacy_erase_confirm'},env)
    with pytest.raises(HTTPException):env['privacy_erase_confirm'](NS(session={}),123,123,'supplied-csrf')
    assert calls[0][1]=='supplied-csrf'

def test_referral_reconcile_warns_only_admins_without_claiming_bonus():
    sent=AsyncMock();env={'referral_rewards':NS(RECONCILE='reconcile',GRANTED='granted'),'config':NS(ADMIN_IDS=[999]),'message_journal':NS(service_message=nullcontext),'bot':NS(send_message=sent),'logger':NS(warning=lambda *a:None)}
    load('main.py',{'send_referral_reward_notification'},env)
    immediate(env['send_referral_reward_notification']({'status':'reconcile','referrer_tg_id':123,'referred_tg_id':124}))
    assert sent.await_count==1
    assert sent.await_args.kwargs['chat_id']==999
    assert 'ручная сверка' in sent.await_args.kwargs['text']
    assert 'Вам начислено' not in sent.await_args.kwargs['text']

def test_secondary_telegram_id_fallback_requires_verified_positive_id():
    from user_identity import normalize_username
    env={'Any':object,'html':html,'normalize_username':normalize_username}
    load('webapp.py',{'_telegram_profile_link'},env);fn=env['_telegram_profile_link']
    assert 'href=' not in fn(123,'name')
    assert 'tg://user?id=123' in fn(123,'name',verified_id=True)
    assert 'href=' not in fn(-123,'local',verified_id=True)
    assert 'href=' not in fn(9007199254740993,'unsafe',verified_id=True)

@pytest.mark.parametrize('name',['get_user_menu_async','get_cabinet_markup_async'])
def test_async_menu_builders_offload_blocking_builder(name):
    calls=[]
    def builder(*args):return 'synthetic keyboard'
    async def to_thread(fn,*args):calls.append(fn);return fn(*args)
    env={'Any':object,'asyncio':NS(to_thread=to_thread),'get_user_menu':builder,'get_cabinet_markup':builder}
    load('main.py',{name},env)
    assert immediate(env[name](123,{'tg_id':123}))=='synthetic keyboard'
    assert calls==[builder]

@pytest.mark.parametrize('value,expected',[('²',0),('12',12),(None,0),('-5',0)])
def test_client_error_coordinates_handle_non_ascii_digit_without_exception(value,expected):
    env={'Any':object};load('webapp.py',{'_client_error_coordinate'},env)
    assert env['_client_error_coordinate'](value)==expected

def test_publisher_configuration_is_restricted_to_publisher_identity():
    env={'Request':object,'update_manager':NS(is_publisher_username=lambda username:username=='menshikovivan')}
    load('webapp.py',{'can_configure_github','update_publisher_for_request'},env)
    for name in ('can_configure_github','update_publisher_for_request'):
        assert env[name](NS(session={'user':'admin'})) is False
        assert env[name](NS(session={'user':'menshikovivan'})) is True

def test_group_callback_is_blocked_before_registration_or_admin_bypass():
    import typing
    tree=ast.parse((APP/'telegram_registration_middleware.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='InvitationAccessMiddleware')
    method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='__call__')
    env={'Any':object,'Callable':typing.Callable,'Awaitable':typing.Awaitable,'Message':type('Message',(),{}),'config':NS(ADMIN_IDS=[123]),'_answer_callback':AsyncMock()}
    load('telegram_registration_middleware.py',{'_parts','update_actor'},env)
    exec(compile(ast.Module(body=[method],type_ignores=[]),'group middleware','exec'),env)
    handler=AsyncMock()
    event=NS(callback_query=NS(from_user=NS(id=123),message=NS(chat=NS(type='supergroup',id=-100))))
    immediate(env['__call__'](None,handler,event,{}))
    assert handler.await_count==0
    assert env['_answer_callback'].await_count==1

@pytest.mark.parametrize('page_no,first_id,shown,has_next',[(1,1,10,True),(11,101,10,True),(13,121,1,False)])
def test_crm_pagination_reaches_records_beyond_old_hundred_row_limit(page_no,first_id,shown,has_next):
    buttons=[];queries=[]
    records=[(i,f'user_{i}','',1,0,'uuid','email','sub','active','') for i in range(1,122)]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args):queries.append(args);self.args=args;return self
        def fetchall(self):limit,offset=self.args;return records[offset:offset+limit]
    class Builder:
        def button(self,**kw):buttons.append(kw)
        def adjust(self,*a):pass
        def as_markup(self):return buttons
    async def to_thread(fn,*args):return fn(*args)
    msg=NS(answer=AsyncMock())
    env={'Message':object,'database_adapter':NS(connect=lambda *a,**kw:Conn()),'config':NS(DB_PATH='unused'),'asyncio':NS(to_thread=to_thread),'billing':NS(family_profile_map=lambda _: {},subscription_label=lambda _: 'active',subscription_status_emoji=lambda _: 'ok',telegram_identity_label=lambda tid,*a:str(tid)),'InlineKeyboardBuilder':Builder,'get_admin_menu':lambda:None}
    load('main.py',{'send_crm_list'},env);immediate(env['send_crm_list'](msg,page_no))
    profiles=[b for b in buttons if b['callback_data'].startswith('crmv_')]
    assert len(profiles)==shown and profiles[0]['callback_data']==f'crmv_{first_id}'
    assert queries==[(11,(page_no-1)*10)]
    assert any(b['callback_data']==f'crm_page:{page_no+1}' for b in buttons)==has_next

def test_bot_operational_delete_refuses_family_before_revoke():
    calls=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,args):calls.append(sql);return self
        def fetchone(self):return (1,)
    env={'subscriptions':NS(_subscription_identity_lock_sync=lambda *a:nullcontext()),'config':NS(DB_PATH='unused'),'database_adapter':NS(connect=lambda *a:Conn()),'delete_client_sync':lambda *a,**kw:pytest.fail('family must be checked first')}
    load('main.py',{'_delete_crm_user_sync'},env)
    with pytest.raises(ValueError):env['_delete_crm_user_sync'](123,'admin')
    assert len(calls)==1



