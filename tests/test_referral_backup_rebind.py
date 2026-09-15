import concurrent.futures
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import backup
import identity_migration
import referral_rewards
import services.subscriptions as subscriptions
import services.xui_api as xui_api
import webapp


def make_db(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT, uuid TEXT, email TEXT, expiry_time INTEGER,
            enable INTEGER, up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
            sub_id TEXT, last_sync_at TEXT, last_reminder_days INTEGER DEFAULT -1,
            referral_code TEXT, referral_code_updated_at INTEGER,
            referred_by_tg_id INTEGER, referred_by_code TEXT, registered_at TEXT,
            identity_source TEXT, identity_updated_at TEXT, notes TEXT
        );
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, username TEXT,
            telegram_file_id TEXT, status TEXT, amount INTEGER, created_at TEXT,
            processed_at TEXT, processed_by TEXT, last_error TEXT, auto_approved INTEGER DEFAULT 0
        );
        CREATE TABLE message_log (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, username TEXT, message TEXT, success INTEGER, detail TEXT, actor TEXT, created_at TEXT);
        CREATE TABLE user_events (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, username TEXT, direction TEXT, event_type TEXT, text TEXT, actor TEXT, success INTEGER, metadata TEXT, created_at TEXT);
        CREATE TABLE user_message_state (tg_id INTEGER PRIMARY KEY, last_read_event_id INTEGER DEFAULT 0, last_incoming_event_id INTEGER DEFAULT 0, unread_count INTEGER DEFAULT 0, last_message_at TEXT, last_message_text TEXT);
        CREATE TABLE referral_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_tg_id INTEGER NOT NULL,
            referred_tg_id INTEGER NOT NULL UNIQUE,
            reward_days INTEGER NOT NULL DEFAULT 10,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            granted_at TEXT,
            target_expiry_ms INTEGER,
            observed_expiry_before_ms INTEGER,
            details TEXT
        );
        CREATE INDEX idx_referral_rewards_referrer ON referral_rewards(referrer_tg_id, id DESC);
        CREATE INDEX idx_referral_rewards_status ON referral_rewards(status, id DESC);
        """
    )
    return connection


def insert_users(conn, rows):
    conn.executemany(
        "INSERT INTO users(tg_id,username,uuid,email,expiry_time,enable,referred_by_tg_id,referred_by_code,referral_code,registered_at) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        rows,
    )
    conn.commit()


def test_referral_first_payment_active_and_repeat_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "vpn.db"
    conn = make_db(db)
    insert_users(conn, [
        (100, "ref", "u100", "ref@example", int(time.time()*1000) + 20*86400000, 1, None, None, "1111"),
        (200, "inv", "u200", "inv@example", 0, 1, 100, "1111", "2222"),
    ])
    conn.execute("INSERT INTO payments(tg_id,status) VALUES(200,'approved')")
    conn.commit(); conn.close()
    state = {"expiry": int(time.time()*1000) + 20*86400000}
    monkeypatch.setattr(referral_rewards, "get_client_record_sync", lambda email: {"client": {"email": email, "expiryTime": state["expiry"], "enable": True}, "normalized": {"expiry_time": state["expiry"]}})
    def update(email, changes, record=None):
        state["expiry"] = int(changes["expiryTime"])
    monkeypatch.setattr(referral_rewards, "update_client_sync", update)
    result = referral_rewards.apply_referral_reward(200, payment_id=1, db_path=db)
    assert result["status"] == referral_rewards.GRANTED
    first = state["expiry"]
    assert first > int(time.time()*1000) + 29*86400000
    again = referral_rewards.apply_referral_reward(200, payment_id=1, db_path=db)
    assert again["already_processed"] is True
    assert state["expiry"] == first
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM referral_rewards WHERE referred_tg_id=200").fetchone()[0] == "granted"
    conn.close()


def test_referral_race_never_adds_more_than_once(tmp_path, monkeypatch):
    db = tmp_path / "vpn.db"
    conn = make_db(db)
    base = int(time.time()*1000) + 20*86400000
    insert_users(conn, [(100,"ref","u100","ref@example",base,1,None,None,"1111"),(200,"inv","u200","inv@example",0,1,100,"1111","2222")])
    conn.execute("INSERT INTO payments(tg_id,status) VALUES(200,'approved')"); conn.commit(); conn.close()
    state = {"expiry": base}
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    calls = {"get": 0}
    def get_record(email):
        with lock:
            calls["get"] += 1
            n = calls["get"]
        if n <= 2:
            barrier.wait(timeout=5)
        with lock:
            return {"client":{"email":email,"expiryTime":state["expiry"],"enable":True},"normalized":{"expiry_time":state["expiry"]}}
    def update(email, changes, record=None):
        with lock:
            state["expiry"] = int(changes["expiryTime"])
    monkeypatch.setattr(referral_rewards, "get_client_record_sync", get_record)
    monkeypatch.setattr(referral_rewards, "update_client_sync", update)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: referral_rewards.apply_referral_reward(200, payment_id=1, db_path=db), range(2)))
    assert sum(r.get("status") == referral_rewards.GRANTED and not r.get("already_processed") for r in results) == 1
    assert state["expiry"] < base + 11*86400000
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT target_expiry_ms,status FROM referral_rewards WHERE referred_tg_id=200").fetchone()
    assert row[1] == "granted"
    assert row[0] == state["expiry"] or row[0] < state["expiry"]
    conn.close()


def test_referral_expired_starts_from_now(tmp_path, monkeypatch):
    db = tmp_path / "vpn.db"; conn = make_db(db)
    insert_users(conn, [(100,"ref","u100","ref@example",1000,1,None,None,"1111"),(200,"inv","u200","inv@example",0,1,100,"1111","2222")])
    conn.execute("INSERT INTO payments(tg_id,status) VALUES(200,'approved')"); conn.commit(); conn.close()
    state={"expiry":1000}
    monkeypatch.setattr(referral_rewards, "get_client_record_sync", lambda e:{"client":{"email":e,"expiryTime":state["expiry"]},"normalized":{"expiry_time":state["expiry"]}})
    monkeypatch.setattr(referral_rewards, "update_client_sync", lambda e,c,record=None: state.update(expiry=int(c["expiryTime"])))
    before=int(time.time()*1000)
    result=referral_rewards.apply_referral_reward(200,db_path=db)
    assert result["status"]=="granted"
    assert state["expiry"] >= before + 9*86400000


def test_referral_unlimited_does_not_change_expiry(tmp_path, monkeypatch):
    db=tmp_path/"vpn.db"; conn=make_db(db)
    insert_users(conn, [(100,"ref","u100","ref@example",0,1,None,None,"1111"),(200,"inv","u200","inv@example",0,1,100,"1111","2222")])
    conn.execute("INSERT INTO payments(tg_id,status) VALUES(200,'approved')"); conn.commit(); conn.close()
    monkeypatch.setattr(referral_rewards, "get_client_record_sync", lambda e:{"client":{"email":e,"expiryTime":0},"normalized":{"expiry_time":0}})
    monkeypatch.setattr(referral_rewards, "update_client_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not update unlimited client')))
    result=referral_rewards.apply_referral_reward(200,db_path=db)
    assert result["status"]==referral_rewards.SKIPPED_UNLIMITED
    conn=sqlite3.connect(db); assert conn.execute("SELECT status FROM referral_rewards WHERE referred_tg_id=200").fetchone()[0]=="skipped_unlimited"; conn.close()


def test_referral_stats_counts_only_actual_grants(tmp_path):
    db=tmp_path/"vpn.db"; conn=make_db(db)
    insert_users(conn, [(100,"ref","u100","ref@example",0,1,None,None,"1111"),(200,"a","u200","a@example",0,1,100,"1111","2222"),(300,"b","u300","b@example",0,1,100,"1111","3333")])
    conn.execute("INSERT INTO referral_rewards(referrer_tg_id,referred_tg_id,status,reward_days) VALUES(100,200,'granted',10),(100,300,'skipped_unlimited',10)")
    conn.commit(); conn.close()
    assert referral_rewards.referral_stats(100, db_path=db) == {"invited":2,"paid":0,"rewards":1,"days":10}


def test_registration_logic_has_no_self_referral_and_preserves_existing_referrer():
    text = Path('main.py').read_text(encoding='utf-8')
    assert 'int(owner.get("tg_id") or 0) == int(user_id)' in text
    assert 'existing_referrer = int(existing[0] or 0) if existing else 0' in text
    assert 'CASE WHEN COALESCE(users.referred_by_tg_id,0)>0 THEN users.referred_by_tg_id' in text


def test_approve_payment_result_carries_referral(monkeypatch, tmp_path):
    db=tmp_path/"vpn.db"; conn=make_db(db)
    conn.execute("INSERT INTO payments(tg_id,username,status) VALUES(200,'inv','pending')")
    conn.execute("INSERT INTO users(tg_id,username) VALUES(200,'inv')")
    conn.commit(); conn.close()
    sub=subscriptions.SubscriptionResult(200,'inv','e','u','s',123,True)
    monkeypatch.setattr(subscriptions, "ensure_subscription_sync", lambda *a, **k: sub)
    monkeypatch.setattr(subscriptions.referral_rewards, "apply_referral_reward", lambda *a, **k: {"status":"not_eligible"})
    result=subscriptions.approve_payment_sync(1,"web",30,db)
    assert result.success is True
    assert result.referral == {"status":"not_eligible"}


def test_identity_rebind_preserves_referral_ledger(tmp_path, monkeypatch):
    db=tmp_path/"vpn.db"; conn=make_db(db)
    insert_users(conn, [(100,'ref','u100','ref@example',0,1,None,None,'1111'),(200,'old','u200','client@example',123,1,100,'1111','2222')])
    conn.execute("INSERT INTO referral_rewards(referrer_tg_id,referred_tg_id,status) VALUES(200,300,'granted')")
    conn.execute("INSERT INTO user_events(tg_id) VALUES(200)")
    conn.commit(); conn.close()
    fake={"client":{"email":"client@example","tgId":200},"normalized":{"uuid":"u200","expiry_time":123,"enable":True,"sub_id":"sub"}}
    monkeypatch.setattr(xui_api, "get_client_record_sync", lambda email: fake)
    monkeypatch.setattr(xui_api, "bind_client_tg_id_sync", lambda email,tg: fake["client"].update(tgId=tg))
    result=identity_migration.bind_existing_panel_client(200,400,'client@example','new',db_path=db)
    assert result['tg_id']==400
    conn=sqlite3.connect(db)
    assert conn.execute("SELECT tg_id FROM users WHERE tg_id=400").fetchone()[0]==400
    assert conn.execute("SELECT referrer_tg_id,referred_tg_id FROM referral_rewards").fetchone()==(400,300)
    assert conn.execute("SELECT tg_id FROM user_events").fetchone()[0]==400
    conn.close()


def test_identity_bind_blocks_existing_owner(tmp_path, monkeypatch):
    db=tmp_path/"vpn.db"; conn=make_db(db)
    insert_users(conn, [(200,'old','u200','client@example',123,1,None,None,'2222'),(300,'other','u300','other@example',123,1,None,None,'3333')])
    conn.commit(); conn.close()
    fake={"client":{"email":"client@example","tgId":200},"normalized":{"uuid":"u200","expiry_time":123,"enable":True,"sub_id":"sub"}}
    monkeypatch.setattr(xui_api, "get_client_record_sync", lambda email: fake)
    monkeypatch.setattr(xui_api, "bind_client_tg_id_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not touch panel when local id occupied')))
    try:
        identity_migration.bind_existing_panel_client(200,300,'client@example',db_path=db)
    except Exception as error:
        assert 'уже используется' in str(error)
    else:
        raise AssertionError('expected occupied id rejection')


def test_yandex_test_upload_delegates_to_production_uploader(monkeypatch, tmp_path):
    archive=tmp_path/'probe.tar.gz'; archive.write_bytes(b'probe')
    calls={}
    def fake_upload(path, token_override=None, directory_override=None):
        calls.update(path=Path(path), token=token_override, directory=directory_override)
        return True,'verified'
    monkeypatch.setattr(backup,'upload_to_yandex',fake_upload)
    class DummyResponse:
        def __init__(self): self.status_code=204
        def raise_for_status(self): return None
    class DummyClient:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def delete(self,*a,**k): return DummyResponse()
    monkeypatch.setattr(backup.httpx, 'Client', DummyClient)
    ok,detail=backup.upload_to_yandex_with_name(archive,'tok','Dir')
    assert ok is True
    assert calls=={'path':archive,'token':'tok','directory':'Dir'}


def test_user_detail_contains_referral_and_bind_ui(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, 'require_auth', lambda request: None)
    monkeypatch.setattr(webapp.config, 'DB_PATH', str(tmp_path / 'vpn.db'), raising=False)
    monkeypatch.setattr(webapp, 'get_user', lambda tg_id: {'tg_id':tg_id,'username':'u','email':'e','uuid':'u','referred_by_tg_id':0,'referral_code':'1111','registered_at':'x','identity_updated_at':'x'})
    monkeypatch.setattr(webapp, 'database', lambda: None)
    # Use direct replacement of the database context with a small context manager.
    class Ctx:
        def __enter__(self):
            class C:
                def execute(self,*a):
                    class R:
                        def fetchone(self): return None
                    return R()
            return C()
        def __exit__(self,*a): pass
    monkeypatch.setattr(webapp, 'database', lambda: Ctx())
    monkeypatch.setattr(webapp, 'fetch_client_extra_sync', lambda email:{'traffic':{},'ips':[],'error':''})
    monkeypatch.setattr(webapp.user_events, 'mark_conversation_opened', lambda *a, **k: None)
    monkeypatch.setattr(webapp.user_events, 'recent_events', lambda *a, **k: [])
    monkeypatch.setattr(webapp, 'page', lambda request,*args: webapp.HTMLResponse(args[1]+args[3]))
    response=webapp.user_detail_page(webapp.Request({'type':'http','method':'GET','path':'/users/id/10','headers':[],'query_string':b'','session':{'user':'admin'}}),10)
    assert 'Реферальная информация' in response.body.decode()
    assert 'Привязать существующий клиент 3x-ui' in response.body.decode()
    assert '/api/users/10/xui-clients' in response.body.decode()


def test_first_approved_payment_really_triggers_referral_reward(tmp_path, monkeypatch):
    db = tmp_path / 'vpn.db'; conn = make_db(db)
    base = int(time.time()*1000) + 5*86400000
    insert_users(conn, [(100,'ref','u100','ref@example',base,1,None,None,'1111'),(200,'inv','u200','inv@example',0,1,100,'1111','2222')])
    conn.execute("INSERT INTO payments(tg_id,username,status) VALUES(200,'inv','pending')")
    conn.commit(); conn.close()
    state = {'expiry': base}
    sub = subscriptions.SubscriptionResult(200,'inv','inv@example','u200','sub200',int(time.time()*1000)+30*86400000,True)
    monkeypatch.setattr(subscriptions, 'ensure_subscription_sync', lambda *a, **k: sub)
    monkeypatch.setattr(subscriptions.referral_rewards, 'get_client_record_sync', lambda email: {'client': {'email':email,'expiryTime':state['expiry'],'enable':True}, 'normalized': {'expiry_time':state['expiry']}})
    monkeypatch.setattr(subscriptions.referral_rewards, 'update_client_sync', lambda email, changes, record=None: state.update(expiry=int(changes['expiryTime'])))
    result = subscriptions.approve_payment_sync(1,'web',30,db)
    assert result.success and result.referral and result.referral['status'] == referral_rewards.GRANTED
    assert state['expiry'] >= base + 9*86400000
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM payments WHERE id=1").fetchone()[0] == 'approved'
    assert conn.execute("SELECT status FROM referral_rewards WHERE referred_tg_id=200").fetchone()[0] == 'granted'
    conn.close()


def test_db_migration_creates_referral_ledger_and_ignores_historical_paid(tmp_path, monkeypatch):
    import init_db
    db = tmp_path / 'migrated.db'
    monkeypatch.setattr(init_db.config, 'DB_PATH', str(db), raising=False)
    init_db.migrate()
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'referral_rewards' in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(referral_rewards)")}
    assert {'referrer_tg_id','referred_tg_id','reward_days','status','granted_at','target_expiry_ms'}.issubset(cols)
    # Populate a historical paid referral and run migration again: no reward is granted.
    conn.execute("INSERT INTO users(tg_id,username,referred_by_tg_id,referred_by_code,referral_code) VALUES(100,'ref',NULL,NULL,'1111')")
    conn.execute("INSERT INTO users(tg_id,username,referred_by_tg_id,referred_by_code,referral_code) VALUES(200,'inv',100,'1111','2222')")
    conn.execute("INSERT INTO payments(tg_id,status) VALUES(200,'approved')")
    conn.commit(); conn.close()
    init_db.migrate()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM referral_rewards WHERE referred_tg_id=200").fetchone()[0] == 'legacy_ignored'
    assert conn.execute("SELECT COALESCE(SUM(reward_days),0) FROM referral_rewards WHERE referrer_tg_id=100 AND status='granted'").fetchone()[0] == 0
    conn.close()
