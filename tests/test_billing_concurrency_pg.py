"""Real PostgreSQL regressions. Only run against an explicitly disposable test DB."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest

DSN = os.getenv('FARGOVPN_TEST_BILLING_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='requires explicit disposable PostgreSQL billing DSN')

@pytest.fixture(scope='module')
def app():
    root = Path(os.getenv('FARGOVPN_BILLING_APP', str(Path(__file__).resolve().parents[1] / 'app'))).resolve()
    original_path = list(sys.path)
    original_dsn = os.environ.get('FARGOVPN_DATABASE_URL')
    original_config = sys.modules.get('config')
    def belongs(module):
        filename = getattr(module, '__file__', None)
        paths = [filename] if filename else list(getattr(module, '__path__', []))
        return any(Path(path).resolve().is_relative_to(root) for path in paths if path)
    # Other tests deliberately import app modules with tiny config/DB doubles.
    # Load a fresh complete graph, then restore those exact module objects so
    # this opt-in integration suite does not alter test order or their globals.
    saved_modules = {name: module for name, module in list(sys.modules.items()) if belongs(module)}
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(root))
    db = None
    try:
        spec = importlib.util.spec_from_file_location('config', root/'config.example.py')
        config = importlib.util.module_from_spec(spec)
        sys.modules['config'] = config
        spec.loader.exec_module(config)
        config.PAYMENT_PRICE = 200
        config.SUBSCRIPTION_DAYS = 30
        config.DB_PATH = 'disposable-billing-regression'
        os.environ['FARGOVPN_DATABASE_URL'] = DSN
        import db, init_db
        init_db.migrate()
        from services import billing, subscriptions, receipt_ocr, payment_submission, xui_api
        import referral_rewards, identity_migration
        yield locals()
    finally:
        if db is not None and db._ENGINE is not None:
            db._ENGINE.dispose()
        for name, module in list(sys.modules.items()):
            if belongs(module):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        if original_config is None:
            sys.modules.pop('config', None)
        else:
            sys.modules['config'] = original_config
        if original_dsn is None:
            os.environ.pop('FARGOVPN_DATABASE_URL', None)
        else:
            os.environ['FARGOVPN_DATABASE_URL'] = original_dsn
        sys.path[:] = original_path

@pytest.fixture
def sample(app):
    db = app['db']
    base = 542000000 + secrets.randbelow(1000000) * 10
    ids = list(range(base, base+8))
    def sql(query, params=()):
        with db.connect() as c:
            cur=c.execute(query,params)
            return cur.fetchall() if query.lstrip().upper().startswith('SELECT') else None
    with db.connect() as c:
        for identity in ids:
            c.execute("INSERT INTO users(tg_id,registration_status,username) VALUES(?,'active',?)",(identity,'synthetic-'+str(identity)))
    yield ids,sql
    with db.connect() as c:
        placeholders=','.join('?' for _ in ids)
        for table, column in [('referral_rewards','referred_tg_id'),('payments','tg_id'),('user_events','tg_id'),('user_message_state','tg_id')]:
            c.execute(f'DELETE FROM {table} WHERE {column} IN ({placeholders})',ids)
        c.execute(f'DELETE FROM users WHERE tg_id IN ({placeholders})',ids)

def test_existing_shell_self_map_accepts_invitation(app,sample):
    ids,sql=sample;b=app['billing'];owner,member=ids[:2]
    sql('INSERT INTO telegram_profile_links(telegram_id,profile_tg_id) VALUES(?,?)',(member,member))
    token=b.create_profile_link_request_sync(owner)
    assert b.approve_profile_link_sync(token,member)==owner
    assert b.profile_owner(member)==owner
    assert b.expected_payment_amount(member)==400
    assert len(b.profile_accounts(owner))==2
    for member, amount in zip(ids[2:4], (600,800)):
        assert b.approve_profile_link_sync(b.create_profile_link_request_sync(owner),member)==owner
        assert b.expected_payment_amount(owner)==amount
    sql('UPDATE users SET custom_payment_price=777 WHERE tg_id=?',(owner,))
    assert b.expected_payment_amount(owner)==777
    sql('UPDATE users SET custom_payment_price=0 WHERE tg_id=?',(owner,))
    assert b.expected_payment_amount(owner)==0

def test_concurrent_family_cap_includes_owner_without_selfmap(app,sample):
    ids,sql=sample;b=app['billing'];owner=ids[0]
    tokens=[b.create_profile_link_request_sync(owner) for _ in range(4)]
    def accept(pair):
        try: b.approve_profile_link_sync(*pair);return True
        except ValueError:return False
    with ThreadPoolExecutor(4) as pool:
        result=list(pool.map(accept,zip(tokens,ids[1:5])))
    assert sum(result)==3,result
    assert b.linked_count(owner)==4
    assert b.expected_payment_amount(owner)==800
    assert len(b.profile_accounts(owner))==4

def test_deleted_member_does_not_bill_and_rekey_preserves_family(app,sample):
    ids,sql=sample;b=app['billing'];owner,member,newid=ids[:3]
    sql('DELETE FROM users WHERE tg_id=?',(newid,))
    sql('INSERT INTO telegram_profile_links(telegram_id,profile_tg_id) VALUES(?,?),(?,?)',(owner,owner,member,owner))
    sql('UPDATE users SET tg_id=? WHERE tg_id=?',(newid,owner))
    ids[0]=newid
    assert b.profile_owner(member)==newid
    assert b.profile_owner(newid)==newid
    sql('DELETE FROM users WHERE tg_id=?',(member,))
    assert b.linked_count(newid)==1
    assert b.expected_payment_amount(newid)==200

def test_merge_refuses_business_conflict_before_data_loss(app,sample):
    ids,sql=sample;m=app['identity_migration'];left,right=ids[:2]
    sql("UPDATE users SET uuid='same-synthetic',notes=CASE WHEN tg_id=? THEN 'left comment' ELSE 'right comment' END WHERE tg_id IN (?,?)",(left,left,right))
    with pytest.raises(m.IdentityImportError,match='Конфликт'):
        m.rebind_local_identity(left,right,None,overwrite_positive=True)
    assert len(sql('SELECT tg_id FROM users WHERE tg_id IN (?,?)',(left,right)))==2

def test_merge_preserves_single_family(app,sample):
    ids,sql=sample;m=app['identity_migration'];left,right,member=ids[:3]
    sql("UPDATE users SET uuid='same-synthetic' WHERE tg_id IN (?,?)",(left,right))
    sql('INSERT INTO telegram_profile_links(telegram_id,profile_tg_id) VALUES(?,?),(?,?),(?,?)',(left,left,right,right,member,left))
    m.rebind_local_identity(left,right,None,overwrite_positive=True)
    assert app['billing'].profile_owner(member)==right
    assert app['billing'].linked_count(right)==2

def test_atomic_receipt_reservation_pending_and_winner_retry(app,sample,monkeypatch):
    ids,sql=sample;r=app['receipt_ocr'];submission=app['payment_submission']
    pids=[submission.submit_receipt_sync(i,'synthetic','file',200,'test-'+secrets.token_hex(16),'synthetic')[0] for i in ids[:4]]
    # Real receipt parsing is replaced at the external OCR boundary only.
    analysis=r.analyze_receipt(b'not-an-image', receiver='synthetic',phone='',minimum_amount=200,ocr_text='synthetic',check_name=False,check_phone=False,check_date=False,check_status=False)
    fingerprint=secrets.token_hex(32)
    analysis=replace(analysis,file_sha256=fingerprint,text_sha256=fingerprint,perceptual_hash='',passed=True,status='passed',reasons=(),amount=200.0)
    monkeypatch.setattr(r,'analyze_receipt',lambda *a,**k:analysis)
    barrier=threading.Barrier(4)
    def run(pid):
        barrier.wait(timeout=10)
        return r.analyze_payment_receipt('synthetic',pid,b'synthetic')
    with ThreadPoolExecutor(4) as pool:
        results=list(pool.map(run,pids))
    winners=[pid for pid,result in zip(pids,results) if result.passed]
    assert len(winners)==1,[a.status for a in results]
    assert r.analyze_payment_receipt('synthetic',winners[0],b'synthetic').passed
    loser=next(pid for pid in pids if pid!=winners[0])
    calls=[]
    monkeypatch.setattr(app['subscriptions'],'ensure_subscription_sync',lambda *a,**k:calls.append(a))
    decision=app['subscriptions'].approve_payment_sync(loser,'test',30)
    assert not decision.success
    assert not calls

def test_parallel_referrals_add_every_bonus_and_ambiguous_retry_stops(app,sample,monkeypatch):
    ids,sql=sample;r=app['referral_rewards'];owner=ids[0];day=86400000
    sql("UPDATE users SET email='synthetic-owner' WHERE tg_id=?",(owner,))
    for referred in ids[1:4]:sql('UPDATE users SET referred_by_tg_id=? WHERE tg_id=?',(owner,referred))
    initial=int(time.time()*1000)+100*day;expiry=[initial]
    monkeypatch.setattr(r,'ensure_schema',lambda c:None)
    def get(email):
        value=expiry[0];time.sleep(.03)
        return {'client':{'expiryTime':value},'normalized':{'expiry_time':value}}
    def update(email,changes,record=None):time.sleep(.03);expiry[0]=changes['expiryTime']
    monkeypatch.setattr(r,'get_client_record_sync',get);monkeypatch.setattr(r,'update_client_sync',update)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda i:r.apply_referral_reward(i),ids[1:3]))
    assert all(result['status']=='granted' for result in results),results
    assert expiry[0]==initial+20*day
    # Simulate remote update accepted, response lost. A later paid renewal must
    # not be counted as proof that this reward was granted on an automatic retry.
    def ambiguous(*args,**kw):
        update(*args,**kw);raise TimeoutError('synthetic response lost')
    monkeypatch.setattr(r,'update_client_sync',ambiguous)
    r.apply_referral_reward(ids[3])
    before=expiry[0]+30*day;expiry[0]=before
    monkeypatch.setattr(r,'update_client_sync',lambda *a,**k:pytest.fail('ambiguous reward retried'))
    result=r.apply_referral_reward(ids[3])
    assert result['status']=='reconcile',result
    assert expiry[0]==before

def test_purchased_duration_snapshot_used_on_approval(app,sample,monkeypatch):
    ids,sql=sample;s=app['subscriptions'];submission=app['payment_submission']
    pid,_=submission.submit_receipt_sync(ids[0],'synthetic','file',200,'test-'+secrets.token_hex(16),'synthetic',purchased_days=45)
    seen=[]
    def provision(tg,username,days,db_path):
        seen.append(days)
        return s.SubscriptionResult(tg,username,'synthetic','uuid','sub',123456,True)
    monkeypatch.setattr(s,'ensure_subscription_sync',provision)
    monkeypatch.setattr(app['referral_rewards'],'apply_referral_reward',lambda *a,**k:{'status':'not_eligible'})
    assert s.approve_payment_sync(pid,'synthetic',days=7).success
    assert seen==[45]

def test_unlimited_cannot_be_shortened_by_ordinary_renewal(app,monkeypatch):
    x=app['xui_api'];updates=[]
    monkeypatch.setattr(x,'get_client_record_sync',lambda *a,**k:{'client':{'expiryTime':0},'normalized':{'expiry_time':0}})
    monkeypatch.setattr(x,'update_client_sync',lambda *a,**k:updates.append(a))
    with pytest.raises(ValueError,match='Бессроч'):
        x.extend_client_sync('synthetic',30)
    assert updates==[]



def test_paid_renewal_and_referral_share_subscription_lock(app,sample,monkeypatch):
    ids,sql=sample;s=app['subscriptions'];r=app['referral_rewards'];owner,referred=ids[:2];day=86400000
    initial=int(time.time()*1000)+100*day;expiry=[initial]
    email='synthetic-'+str(owner)
    sql("UPDATE users SET email=?,uuid='synthetic-uuid',expiry_time=? WHERE tg_id=?",(email,initial,owner))
    sql('UPDATE users SET referred_by_tg_id=? WHERE tg_id=?',(owner,referred))
    monkeypatch.setattr(r,'ensure_schema',lambda c:None)
    def normalized():return {'email':email,'uuid':'synthetic-uuid','expiry_time':expiry[0],'enable':True,'sub_id':'synthetic-sub'}
    def get(*a,**k):
        value=expiry[0];time.sleep(.03)
        return {'client':{'expiryTime':value},'normalized':normalized()}
    def extend(email,days,tg_id=None):
        value=expiry[0];time.sleep(.08);expiry[0]=value+days*day
        return normalized()
    monkeypatch.setattr(s,'_candidate_for_subscription',lambda *a,**k:normalized())
    monkeypatch.setattr(s,'extend_client_sync',extend)
    monkeypatch.setattr(r,'get_client_record_sync',get)
    monkeypatch.setattr(r,'update_client_sync',lambda email,changes,record=None:expiry.__setitem__(0,changes['expiryTime']))
    with ThreadPoolExecutor(2) as pool:
        paid=pool.submit(s.ensure_subscription_sync,owner,None,30)
        bonus=pool.submit(r.apply_referral_reward,referred)
        assert paid.result().expiry_time>=initial+30*day
        assert bonus.result()['status']=='granted'
    assert expiry[0]==initial+40*day


def test_remote_comment_does_not_overwrite_local_notes(app,sample):
    ids,sql=sample;s=app['subscriptions'];owner=ids[0]
    sql("UPDATE users SET notes='local operator comment' WHERE tg_id=?",(owner,))
    value=s._upsert_user(owner,'synthetic',{'email':'synthetic-'+str(owner),'uuid':'synthetic-uuid','expiry_time':123456,'comment':'remote panel comment'})
    assert value['notes']=='local operator comment'
    assert value['remote_comment']=='remote panel comment'


def test_pending_payment_then_family_join_stays_pending_for_review(app,sample,monkeypatch):
    ids,sql=sample;b=app['billing'];s=app['subscriptions'];owner,member=ids[:2]
    pid,_=app['payment_submission'].submit_receipt_sync(member,'synthetic','file',200,'test-'+secrets.token_hex(16),'synthetic')
    token=b.create_profile_link_request_sync(owner)
    assert b.approve_profile_link_sync(token,member)==owner
    calls=[]
    def provision(tg,username,days,db_path):
        calls.append(tg)
        return s.SubscriptionResult(tg,username,'synthetic','uuid','sub',123456,True)
    monkeypatch.setattr(s,'ensure_subscription_sync',provision)
    decision=s.approve_payment_sync(pid,'synthetic')
    assert not decision.success and 'ручная сверка' in decision.message
    assert not calls
    row=sql('SELECT status,tg_id,expected_amount,last_error FROM payments WHERE id=?',(pid,))[0]
    assert tuple(row)[:3]==('pending',member,200)
    assert 'Доступ не выдавался' in row[3]


def test_family_amount_change_after_receipt_stays_pending_for_review(app,sample,monkeypatch):
    ids,sql=sample;b=app['billing'];s=app['subscriptions'];owner,member=ids[:2]
    pid,_=app['payment_submission'].submit_receipt_sync(owner,'synthetic','file',200,'test-'+secrets.token_hex(16),'synthetic')
    b.approve_profile_link_sync(b.create_profile_link_request_sync(owner),member)
    assert b.expected_payment_amount(owner)==400
    calls=[]
    def provision(tg,username,days,db_path):
        calls.append(tg)
        return s.SubscriptionResult(tg,username,'synthetic','uuid','sub',123456,True)
    monkeypatch.setattr(s,'ensure_subscription_sync',provision)
    assert not s.approve_payment_sync(pid,'synthetic').success
    assert not calls
    row=sql('SELECT status,expected_amount FROM payments WHERE id=?',(pid,))[0]
    assert tuple(row)==('pending',200)


def test_family_join_cannot_race_in_flight_activation(app,sample,monkeypatch):
    ids,sql=sample;b=app['billing'];s=app['subscriptions'];owner,member=ids[:2]
    pid,_=app['payment_submission'].submit_receipt_sync(member,'synthetic','file',200,'test-'+secrets.token_hex(16),'synthetic')
    token=b.create_profile_link_request_sync(owner)
    started=threading.Event();release=threading.Event()
    def provision(tg,username,days,db_path):
        started.set();assert release.wait(15)
        return s.SubscriptionResult(tg,username,'synthetic','uuid','sub',123456,True)
    monkeypatch.setattr(s,'ensure_subscription_sync',provision)
    monkeypatch.setattr(app['referral_rewards'],'apply_referral_reward',lambda *a,**k:{'status':'not_eligible'})
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(s.approve_payment_sync,pid,'synthetic')
        try:
            assert started.wait(15)
            with pytest.raises(ValueError,match='обрабатывается'):
                b.approve_profile_link_sync(token,member)
            assert b.profile_owner(member)==member
        finally:release.set()
        assert pending.result().success
