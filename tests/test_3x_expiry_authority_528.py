import importlib.util
import sqlite3
import sys
from pathlib import Path
import pytest
import httpx

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location('config', APP / 'config.example.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
sys.modules['config'] = config
from services import xui_api

@pytest.mark.parametrize('record,expected', [
    ({'client':{'expiryTime':0},'traffic':{'expiryTime':1800000000000}},0),
    ({'client':{'expiryTime':1900000000000},'traffic':{'expiryTime':0}},1900000000000),
    ({'client':{},'traffic':{'expiryTime':1800000000000}},1800000000000),
    ({'expiryTime':0,'traffic':{'expiryTime':1800000000000}},0),
])
def test_client_expiry_authoritative_and_zero_not_overwritten(record,expected):
    assert xui_api.normalize_client(record)['expiry_time']==expected
    assert xui_api._has_expiry_value(record)

def test_missing_expiry_allows_fallback():
    assert not xui_api._has_expiry_value({'client':{'email':'one'},'traffic':{}})

@pytest.mark.parametrize('bad', [None,True,1.2,'abc','-10',-10,[],{}])
def test_invalid_expiry_never_becomes_unlimited(monkeypatch,bad):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'obj':[{'email':'one@test','expiryTime':bad}]})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('invalid expiry must not sync'))
    assert xui_api.fetch_and_sync(force=True,db_path='unused')['stale'] is True

@pytest.mark.parametrize('method', ['extend_client_sync','change_client_days_sync'])
def test_pending_activation_never_mutated_by_generic_days(monkeypatch,method):
    monkeypatch.setattr(xui_api,'get_client_record_sync',lambda email:{'client':{'expiryTime':-86400000},'normalized':{}})
    monkeypatch.setattr(xui_api,'update_client_sync',lambda *a,**k:pytest.fail('unexpected remote write'))
    with pytest.raises(ValueError,match='первого подключения'):
        getattr(xui_api,method)('one@test',30)

def test_unknown_expiry_without_fallback_does_not_sync(monkeypatch):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'obj':[{'email':'one@test'}]})
    monkeypatch.setattr(xui_api,'request_json_sync',lambda *a,**k:{'obj':[]})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('unknown expiry must not sync'))
    assert xui_api.fetch_and_sync(force=True,db_path='unused')['stale'] is True

@pytest.mark.parametrize('bad_obj',[None,{},[None],[{}],[{'settings':None}],[{'settings':'not-json'}],[{'protocol':'vless','settings':{}}],[{'protocol':'vmess','settings':{}}],[{'protocol':'trojan','settings':{}}],[{'protocol':'vless','settings':{'clients':[None]}}]])
def test_legacy_fallback_malformed_inbounds_never_sync(monkeypatch,bad_obj):
    req=httpx.Request('GET','http://test/list');resp=httpx.Response(404,request=req)
    def request(method,path,**kw):
        if path=='panel/api/clients/list': raise httpx.HTTPStatusError('not found',request=req,response=resp)
        return {'obj':bad_obj}
    monkeypatch.setattr(xui_api,'request_json_sync',request)
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('invalid fallback must not sync'))
    assert xui_api.fetch_and_sync(force=True,db_path='unused')['stale'] is True

def test_legacy_fallback_valid_empty(monkeypatch):
    req=httpx.Request('GET','http://test/list');resp=httpx.Response(404,request=req)
    def request(method,path,**kw):
        if path=='panel/api/clients/list': raise httpx.HTTPStatusError('not found',request=req,response=resp)
        return {'obj':[]}
    monkeypatch.setattr(xui_api,'request_json_sync',request)
    assert xui_api.fetch_snapshot_sync(force=True)['stale'] is False

def test_invalid_client_list_is_stale_and_never_synced(monkeypatch):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'success':True,'obj':None})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('invalid snapshot must never sync'))
    result=xui_api.fetch_and_sync(force=True,db_path='unused')
    assert result['stale'] is True

@pytest.mark.parametrize('bad_row', [None, 'bad', {}, {'client':{}}, {'email':'   '}])
def test_incomplete_client_list_is_stale_never_synced(monkeypatch,bad_row):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'success':True,'obj':[{'email':'valid@test'},bad_row]})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('incomplete snapshot must never sync'))
    result=xui_api.fetch_and_sync(force=True,db_path='unused')
    assert result['stale'] is True

def test_empty_client_list_remains_valid(monkeypatch):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'success':True,'obj':[]})
    monkeypatch.setattr(xui_api,'request_json_sync',lambda *a,**k:{'obj':[]})
    result=xui_api.fetch_snapshot_sync(force=True)
    assert result['stale'] is False and result['clients']==[]

def test_empty_list_does_not_disable_existing_profiles(tmp_path,monkeypatch,caplog):
    path=tmp_path/'empty.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE users(tg_id INTEGER PRIMARY KEY,username TEXT,uuid TEXT,email TEXT,expiry_time INTEGER,enable INTEGER,last_reminder_days INTEGER,notes TEXT)')
        db.execute("INSERT INTO users VALUES(1,'one','u1','one@test',1900000000000,1,3,'n')")
    def connect(path,timeout=20): return sqlite3.connect(path,timeout=timeout)
    monkeypatch.setattr(xui_api.database_adapter,'connect',connect)
    monkeypatch.setattr(xui_api,'_ensure_user_columns',lambda db:None)
    assert xui_api.sync_snapshot_to_db({'clients':[]},db_path=path)==0
    with sqlite3.connect(path) as db: assert db.execute('SELECT expiry_time,enable FROM users').fetchone()==(1900000000000,1)
    assert 'пустой список' in caplog.text

@pytest.mark.parametrize('one,two', [(0,1900000000000),(1900000000000,1800000000000)])
def test_conflicting_duplicate_expiry_never_synchronizes(monkeypatch,one,two):
    raw=[{'email':'one@test','uuid':'u1','expiryTime':one}, {'email':'one@test','uuid':'u1','expiryTime':two}]
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'obj':raw})
    monkeypatch.setattr(xui_api,'request_json_sync',lambda *a,**k:{'obj':[]})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('conflicting duplicate must never sync'))
    result=xui_api.fetch_and_sync(force=True,db_path='unused')
    assert result['stale'] is True and 'разными сроками' in result['error']

def test_duplicate_inbounds_with_equal_expiry_merge_traffic():
    one={'email':'one@test','uuid':'u1','expiryTime':1900000000000,'up':10}
    two={'email':'one@test','uuid':'u1','expiryTime':1900000000000,'up':20}
    rows,count=xui_api.deduplicate_clients([xui_api.normalize_client(one),xui_api.normalize_client(two)])
    assert count==1 and rows[0]['expiry_time']==1900000000000 and rows[0]['up']==20

def test_duplicate_explicit_zero_wins_over_missing_expiry():
    unknown=xui_api.normalize_client({'email':'one@test','uuid':'u1'})
    unknown['expiry_time']=1900000000000
    unlimited=xui_api.normalize_client({'email':'one@test','uuid':'u1','expiryTime':0})
    rows,count=xui_api.deduplicate_clients([unknown,unlimited])
    assert count==1 and rows[0]['expiry_time']==0

def test_case_distinct_email_never_merges_even_equal_expiry():
    one=xui_api.normalize_client({'email':'ONE@test','uuid':'u1','expiryTime':0})
    two=xui_api.normalize_client({'email':'one@test','uuid':'u2','expiryTime':0})
    with pytest.raises(ValueError,match='регистров'): xui_api.deduplicate_clients([one,two])

def test_duplicate_conflicting_telegram_id_never_merges():
    one=xui_api.normalize_client({'email':'one@test','uuid':'u1','tgId':1,'expiryTime':0})
    two=xui_api.normalize_client({'email':'one@test','uuid':'u2','tgId':2,'expiryTime':0})
    with pytest.raises(ValueError,match='Telegram'): xui_api.deduplicate_clients([one,two])

def test_real_snapshot_does_not_replace_client_zero_with_stale_inbound_stats(monkeypatch):
    raw=[{'email':'one@test','uuid':'u1','expiryTime':0,'traffic':{'expiryTime':1900000000000}},
         {'email':'two@test','uuid':'u2','expiryTime':1900000000000,'traffic':{'expiryTime':0}},
         {'email':'three@test','uuid':'u3'}]
    stats=[{'email':'one@test','expiryTime':1900000000000}, {'email':'two@test','expiryTime':0},
           {'email':'three@test','expiryTime':1800000000000}]
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'obj':raw})
    monkeypatch.setattr(xui_api,'request_json_sync',lambda method,path,**kw: {'obj':[{'clientStats':stats}]} if path=='panel/api/inbounds/list' else {'obj':[]})
    result=xui_api.fetch_snapshot_sync(force=True)
    assert not result['stale']
    assert [item['expiry_time'] for item in result['clients']]==[0,1900000000000,1800000000000]

@pytest.mark.parametrize('local,panel', [(1700000000000,0),(0,1800000000000),(1900000000000,1800000000000)])
def test_3x_expiry_updates_fargo_existing_profile(tmp_path, monkeypatch, local, panel):
    path = tmp_path / 'test.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE users(tg_id INTEGER PRIMARY KEY,username TEXT,uuid TEXT,email TEXT,
          expiry_time INTEGER,enable INTEGER,up INTEGER,down INTEGER,total INTEGER,sub_id TEXT,
          last_reminder_days INTEGER,notes TEXT,last_online TEXT,last_online_ts INTEGER,last_sync_at TEXT,
          identity_source TEXT,identity_updated_at TEXT,display_name TEXT,registered_at TEXT,registration_status TEXT)''')
        db.execute('INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,up,down,total,sub_id,last_reminder_days,notes) VALUES(1,?,?,?,?,1,0,0,0,?,7,?)',('tester','u1','one@example.test',local,'s','note'))
    def connect(path,timeout=20):
        db=sqlite3.connect(path,timeout=timeout)
        db.create_function('GREATEST',-1,lambda *v:max(x for x in v if x is not None))
        return db
    monkeypatch.setattr(xui_api.database_adapter,'connect',connect)
    xui_api.sync_snapshot_to_db({'clients':[{'tg_id':1,'email':'one@example.test','uuid':'u1','expiry_time':panel,'enable':True,'up':3,'down':0,'total':0,'sub_id':'s'}]}, db_path=path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT expiry_time,last_reminder_days,up FROM users').fetchone()==(panel,-1,3)
