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

@pytest.mark.parametrize('bad', [None, True, 1.2, 'abc', '--10', 2**63, -(2**63), [], {}])
def test_invalid_expiry_never_becomes_unlimited(monkeypatch,bad):
    monkeypatch.setattr(xui_api,'_client_list_sync',lambda:{'obj':[{'email':'one@test','expiryTime':bad}]})
    monkeypatch.setattr(xui_api,'sync_snapshot_to_db',lambda *a,**k:pytest.fail('invalid expiry must not sync'))
    assert xui_api.fetch_and_sync(force=True,db_path='unused')['stale'] is True


def test_deferred_expiry_is_readable_without_obscuring_other_clients(monkeypatch):
    rows = [
        {'email': 'pending@test', 'uuid': 'u1', 'expiryTime': '-86400000'},
        {'email': 'finite@test', 'uuid': 'u2', 'expiryTime': 1_900_000_000_000},
        {'email': 'unlimited@test', 'uuid': 'u3', 'expiryTime': 0},
    ]
    monkeypatch.setattr(xui_api, '_client_list_sync', lambda: {'obj': rows})
    monkeypatch.setattr(xui_api, 'request_json_sync', lambda *a, **kw: {'obj': []})
    result = xui_api.fetch_snapshot_sync(force=True)
    assert result['stale'] is False
    assert [row['expiry_time'] for row in result['clients']] == [-86_400_000, 1_900_000_000_000, 0]

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
        db.execute("INSERT INTO users(tg_id,uuid,email,expiry_time,enable,last_reminder_days) VALUES(2,'u2','pending@test',1800000000000,1,3)")
    def connect(path,timeout=20):
        db=sqlite3.connect(path,timeout=timeout)
        db.create_function('GREATEST',-1,lambda *v:max(x for x in v if x is not None))
        return db
    monkeypatch.setattr(xui_api.database_adapter,'connect',connect)
    records = [
        {'tg_id':1,'email':'one@example.test','uuid':'u1','expiry_time':panel,'enable':True,'up':3,'down':0,'total':0,'sub_id':'s'},
        {'tg_id':2,'email':'pending@test','uuid':'u2','expiry_time':-86_400_000,'enable':False},
    ]
    xui_api.sync_snapshot_to_db({'clients': records}, db_path=path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT expiry_time,last_reminder_days,up FROM users WHERE tg_id=1').fetchone()==(panel,-1,3)
        assert db.execute('SELECT expiry_time,last_reminder_days,enable FROM users WHERE tg_id=2').fetchone()==(1800000000000,3,1)


@pytest.mark.parametrize("expiry", [0, 1900000000000])
def test_comment_update_preserves_expiry_from_get_traffic(monkeypatch, expiry):
    calls = []
    client = {"email": "one@test", "uuid": "test-id", "comment": "", "enable": False,
              "limitIp": 2, "reset": 1, "security": "auto", "subId": "test-sub", "tgId": 42,
              "totalGB": 25, "keepAlive": "15-25"}

    def request(method, path, payload=None):
        if method == "GET":
            return {"obj": {"client": client, "traffic": {"expiryTime": expiry}}}
        calls.append(payload)
        return {"success": True}

    monkeypatch.setattr(xui_api, "request_json_sync", request)
    record = xui_api.get_client_record_sync("one@test")
    assert record["normalized"]["expiry_time"] == expiry
    xui_api.update_client_sync("one@test", {"comment": "Имя"}, record=record)
    assert len(calls) == 1
    assert calls[0]["expiryTime"] == expiry and calls[0]["comment"] == "Имя"
    assert calls[0]["enable"] is False and calls[0]["limitIp"] == 2 and calls[0]["keepAlive"] == "15-25"


def test_days_write_keeps_exact_short_expiry_and_distinct_peer_settings(monkeypatch):
    client = {
        "email": "one@test", "uuid": "test-id", "comment": "", "enable": True,
        "limitIp": 2, "reset": 1, "security": "auto", "subId": "test-sub", "tgId": 42,
        "totalGB": 25, "expiryTime": 86_400_000_001, "keepAlive": "15-25",
        "allowedIPs": "10.0.0.2/32", "privateKey": "peer-private", "publicKey": "peer-public",
        "preSharedKey": "peer-pre-shared", "forwardedPorts": "80",
    }
    writes = []

    def request(method, path, payload=None):
        if method == "POST":
            writes.append(payload)
            client.update({"expiryTime": payload["expiryTime"], "enable": payload["enable"]})
            return {"success": True}
        return {"obj": {"client": client.copy(), "inboundIds": [1, 2],
                        "tunnelAllowedIPs": {"1": "10.0.0.2/32", "2": "10.1.0.2/32"}}}

    monkeypatch.setattr(xui_api, "request_json_sync", request)
    result = xui_api.change_client_days_sync("one@test", -1001)
    assert len(writes) == 1
    assert writes[0]["expiryTime"] == 1 and writes[0]["enable"] is False
    assert not {"keepAlive", "allowedIPs", "privateKey", "publicKey", "preSharedKey", "forwardedPorts"} & writes[0].keys()
    assert result["enable"] is False


@pytest.mark.parametrize("traffic", [{}, {"expiryTime": None}, {"expiryTime": "unexpected"}])
def test_unknown_record_expiry_blocks_update_even_on_explicit_change(monkeypatch, traffic):
    def request(method, path, payload=None):
        assert method == "GET"
        return {"obj": {"client": {"email": "one@test"}, "traffic": traffic}}

    monkeypatch.setattr(xui_api, "request_json_sync", request)
    with pytest.raises(ValueError, match="срок"):
        xui_api.update_client_sync("one@test", {"expiryTime": 1900000000000})


def test_read_only_record_lookup_still_returns_attachments_without_expiry(monkeypatch):
    monkeypatch.setattr(xui_api, "request_json_sync", lambda *a, **kw: {
        "obj": {"client": {"email": "one@test"}, "inboundIds": [17, 19]},
    })
    record = xui_api.get_client_record_sync("one@test")
    assert record["normalized"]["expiry_time"] is None
    assert record["inbound_ids"] == [17, 19]
    assert "expiryTime" not in record["editable_client"]


def test_valid_expiry_alone_does_not_authorize_full_update_of_partial_record(monkeypatch):
    def request(method, path, payload=None):
        assert method == "GET"
        return {"obj": {"client": {"email": "one@test", "expiryTime": 1900000000000}}}

    monkeypatch.setattr(xui_api, "request_json_sync", request)
    with pytest.raises(ValueError, match="неполную карточку"):
        xui_api.update_client_sync("one@test", {"comment": "Имя"})


@pytest.mark.parametrize("existing_comment, incoming_comment, expect_write, conflict", [
    ("", "", True, False),
    ("", "Имя", True, False),
    ("Ручная заметка", "", False, False),
    ("", "Ручная заметка", False, True),
])
def test_local_comment_backfill_updates_only_blank_central_column(
    monkeypatch, tmp_path, existing_comment, incoming_comment, expect_write, conflict
):
    import json
    from contextlib import closing

    db_path = tmp_path / "x-ui.sqlite"
    settings = json.dumps({"clients": [{"email": "one@test", "expiryTime": 1900000000000,
                                       "keepAlive": "15-25", "comment": incoming_comment}]})
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript("""
            CREATE TABLE clients(id INTEGER PRIMARY KEY, email TEXT, uuid TEXT, sub_id TEXT,
                                 tg_id INTEGER, comment TEXT, expiry_time INTEGER, enable INTEGER);
            CREATE TABLE inbounds(id INTEGER PRIMARY KEY, settings TEXT);
            CREATE TABLE client_inbounds(client_id INTEGER, inbound_id INTEGER);
            INSERT INTO client_inbounds VALUES(1,7);
        """)
        db.execute("INSERT INTO clients VALUES(1,'one@test','unique','sub',42,?,1900000000000,0)",
                   (existing_comment,))
        db.execute("INSERT INTO inbounds VALUES(7,?)", (settings,))
    local_base = "http://127.0.0.1:2053"
    monkeypatch.setattr(xui_api.config, "XUI_DB_PATH", str(db_path), raising=False)
    monkeypatch.setattr(xui_api, "_db_xui_internal_base_url", lambda: local_base)
    monkeypatch.setattr(xui_api, "_base_url", lambda: local_base)
    monkeypatch.setattr(xui_api, "_xui_postgres_dsn", lambda: "")
    def client_lookup(email, *, local_base_url):
        assert (email, local_base_url) == ("one@test", local_base)
        return {"client": {"id": 1, "email": email, "uuid": "unique", "subId": "sub", "tgId": 42,
                           "comment": ""},
                "inbound_ids": [7]}

    monkeypatch.setattr(xui_api, "get_client_record_sync", client_lookup)
    if conflict:
        with pytest.raises(RuntimeError, match="другой комментарий"):
            xui_api.fill_empty_local_client_comment_sync("one@test", "Имя")
    else:
        assert xui_api.fill_empty_local_client_comment_sync("one@test", "Имя") is expect_write
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT comment, expiry_time, enable FROM clients").fetchone() == (
            "Имя" if expect_write else existing_comment, 1900000000000, 0,
        )
        assert db.execute("SELECT settings FROM inbounds").fetchone() == (settings,)


def test_comment_backfill_cannot_use_remote_api_fallback(monkeypatch):
    monkeypatch.setattr(xui_api, "_db_xui_internal_base_url", lambda: "http://127.0.0.1:2053")
    monkeypatch.setattr(xui_api, "_base_url", lambda: "https://remote.example")
    monkeypatch.setattr(xui_api, "get_client_record_sync", lambda *a, **kw: pytest.fail("unexpected API read"))
    with pytest.raises(RuntimeError, match="локальной"):
        xui_api.fill_empty_local_client_comment_sync("one@test", "Имя")


def test_repair_fills_blank_comment_even_when_email_already_correct(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from services import subscriptions

    panel_email = subscriptions.panel_email_for("alice", 42)
    panel_client = {"tg_id": 42, "email": panel_email, "uuid": "test-id", "comment": ""}
    local = {"tg_id": 42, "username": "alice", "email": panel_email, "uuid": "test-id", "notes": "Имя"}
    calls = []

    @contextmanager
    def connection(*args):
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [local]))

    monkeypatch.setattr(subscriptions, "_connect", connection)
    monkeypatch.setattr(subscriptions, "fetch_snapshot_sync", lambda **kw: {"clients": [panel_client]})
    monkeypatch.setattr(subscriptions, "get_client_record_sync",
                        lambda email: {"client": {"tgId": 42}, "normalized": panel_client})
    monkeypatch.setattr(xui_api, "fill_empty_local_client_comment_sync",
                        lambda email, comment: calls.append((email, comment)) or True)
    monkeypatch.setattr(xui_api, "update_client_sync", lambda *a, **kw: pytest.fail("full update for comment"))
    result = subscriptions.repair_panel_client_identities_sync()
    assert result["updated"] == 1 and not result["errors"]
    assert calls == [(panel_email, "Имя")]
