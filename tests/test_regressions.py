"""Behavior tests for the 4.9.3 fixes. External Telegram calls are mocked."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import sys
import threading
import types
from unittest.mock import AsyncMock
import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), namespace)
    return namespace


def test_concurrent_different_receipts_and_approved_duplicate(tmp_path, monkeypatch):
    from services import payment_submission as payments
    path = tmp_path / 'payments.db'
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY,tg_id INTEGER,username TEXT,telegram_file_id TEXT,amount INTEGER,ocr_status TEXT,purchase_token TEXT UNIQUE,status TEXT DEFAULT 'pending')")
    lock = threading.Lock()
    class Connection:
        def __init__(self, *args): self.conn = sqlite3.connect(path)
        def __enter__(self): return self
        def execute(self, sql, params=()):
            if 'pg_advisory_xact_lock' in sql:
                lock.acquire()
                return None
            return self.conn.execute(sql, params)
        def __exit__(self, kind, value, trace):
            self.conn.rollback() if kind else self.conn.commit()
            self.conn.close()
            lock.release()
    monkeypatch.setattr(payments.db, 'connect', Connection)
    submit = lambda i: payments.submit_receipt_sync(1, 'alice', f'photo-{i}', 150, 'purchase-one', str(path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(20)))
    assert sum(created for _, created in results) == 1
    assert len({payment_id for payment_id, _ in results}) == 1
    assert not payments.submit_receipt_sync(1, 'alice', 'photo-21', 150, 'new-token', str(path))[1]
    with sqlite3.connect(path) as connection: connection.execute("UPDATE payments SET status='approved'")
    assert not submit(22)[1]
    assert payments.submit_receipt_sync(1, 'alice', 'new-payment', 150, 'new-token', str(path))[1]
    assert payments.submit_receipt_sync(2, 'bob', 'other-user', 150, 'bob-token', str(path))[1]


@pytest.mark.parametrize('user,active', [
    (None, False), ({'enable': 1, 'expiry_time': 0}, False),
    ({'uuid': 'existing', 'enable': 1, 'expiry_time': 0}, True),
    ({'uuid': 'existing', 'enable': 0, 'expiry_time': 0}, False),
    ({'uuid': 'existing', 'enable': 1, 'expiry_time': 99}, False),
    ({'uuid': 'existing', 'enable': 1, 'expiry_time': 101}, True),
])
def test_subscription_presence(user, active):
    from services.subscription_status import has_active_subscription
    assert has_active_subscription(user, now_ms=100) is active


def test_new_user_menu_and_stats():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from services.subscription_status import has_active_subscription
    ns = functions('main.py', ['get_user_menu', 'send_user_stats'], {
        'Message': object, 'InlineKeyboardBuilder': InlineKeyboardBuilder,
        'has_active_subscription': has_active_subscription,
        'config': types.SimpleNamespace(ADMIN_IDS=[]),
        'get_personal_cabinet_url': lambda *args: '',
        'resolve_user': AsyncMock(return_value={'enable': 1, 'expiry_time': 0}),
        'get_client_from_panel': AsyncMock(return_value=None),
    })
    menu = ns['get_user_menu'](1, {'enable': 1, 'expiry_time': 0})
    assert len(menu.inline_keyboard[0]) == 1
    assert menu.inline_keyboard[0][0].text == '💳 Купить подписку'
    active = ns['get_user_menu'](1, {'uuid': 'paid', 'enable': 1, 'expiry_time': 0})
    assert any('Продлить' in b.text for row in active.inline_keyboard for b in row)
    answer = AsyncMock()
    asyncio.run(ns['send_user_stats'](types.SimpleNamespace(answer=answer), 1, 'alice'))
    assert 'Активной подписки нет' in answer.call_args.args[0]
    assert 'оплатите' in answer.call_args.args[0]
    assert answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'menu:buy'


def test_release_section_and_no_history():
    ns = functions('update_manager.py', ['current_release_changelog', 'github_install_command', 'github_notes'], {'re': re, 'sanitize_public_release_text': lambda text: text, 'github_owner': lambda: 'Menshikovivan', 'github_repo': lambda: 'FargoVPN'})
    history = '## 5.0.0 — fixes\n\nNEW FIX\n### Tests\npassed\n\n## 4.9.2 — old\nOLD FIX'
    notes = ns['github_notes'](history, '5.0.0', 'a'*64, 10)
    assert 'NEW FIX' in notes and 'OLD FIX' not in notes
    assert ns['current_release_changelog'](history, '9.9.9') == ''
    assert 'NEW' in ns['current_release_changelog']('## [4.9.3] - 2026-10-03\nNEW', '4.9.3')


def test_photo_conversion():
    from PIL import Image
    from services.media import prepare_telegram_photo
    source = BytesIO()
    Image.new('RGBA', (100, 50), (10, 20, 30, 100)).save(source, 'TIFF')
    prepared, name, mime = prepare_telegram_photo(source, 'upload.tiff')
    assert name == 'upload.jpg' and mime == 'image/jpeg'
    with Image.open(prepared) as image: assert image.format == 'JPEG' and image.mode == 'RGB'
    assert not source.closed
    with pytest.raises(RuntimeError): prepare_telegram_photo(BytesIO(b'not-an-image'), 'fake.jpg')


def test_http_media_auth_prefix_and_filter_ui(tmp_path):
    script = r'''
import base64,importlib.util,json,logging,sys
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from PIL import Image
from itsdangerous import TimestampSigner
root=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('config',root/'config.example.py')
config=importlib.util.module_from_spec(spec);sys.modules['config']=config;spec.loader.exec_module(config)
config.BOT_TOKEN='fake-test-token';config.WEB_PUBLIC_PREFIX='/test-panel';config.DB_PATH='unused'
import psutil
psutil.net_io_counters=lambda:type('Counters',(),{'bytes_recv':0,'bytes_sent':0})()
with patch('logging.FileHandler',lambda *a,**k:logging.NullHandler()):import webapp as web
from fastapi.testclient import TestClient
web.get_user=lambda tg_id:{'tg_id':tg_id,'username':'alice'}
web.audit=lambda *args:None;web._safe_message_log=lambda *args:None
stored={}
def journal(tg_id,**kw):
 stored[7]={'id':7,'tg_id':tg_id,'direction':'out','text':kw['text'],'metadata':kw.get('metadata') or {}}
 return 7
web._safe_journal_outgoing_sync=journal
web.user_events.get_event=lambda tg_id,event_id,**kw:stored.get(event_id) if tg_id==1 else None
web.user_events.recent_events=lambda tg_id,**kw:list(stored.values())
file=BytesIO();Image.new('RGB',(30,30),'blue').save(file,'TIFF')
def post(url,**kw):
 assert url.endswith('/sendPhoto')
 name,stream,mime=kw['files']['photo'];assert name.endswith('.jpg') and mime=='image/jpeg'
 with Image.open(stream) as image:assert image.format=='JPEG'
 assert kw['data']['chat_id']==1 and kw['data']['caption']=='caption'
 return type('Reply',(),{'status_code':200,'json':lambda self:{'ok':True,'result':{'message_id':99,'photo':[{'file_id':'telegram-photo','width':30,'height':30}]}}})()
web.httpx.post=post
client=TestClient(web.app)
assert client.get('/test-panel/api/users/1/events/7/media').status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();client.cookies.set('session',cookie)
r=client.post('/test-panel/users/1/message',data={'message':'caption'},files={'media':('photo.tiff',file.getvalue(),'image/tiff')},headers={'Accept':'application/json','X-CSRF-Token':'csrf'})
assert r.status_code==200,r.text
assert r.json()['event']['metadata']['media']['url']=='/test-panel/api/users/1/events/7/media'
stored[8]={**stored[7],'id':8,'direction':'in'}
feed=client.get('/test-panel/api/users/1/events').json()['events']
assert all(e['metadata']['media']['url'].startswith('/test-panel/api/') for e in feed)
media_path=Path.cwd()/'cached-photo.jpg';Image.new('RGB',(30,30)).save(media_path,'JPEG')
def download(token,file_id,destination,**kw):
 assert file_id=='telegram-photo';Path(destination).write_bytes(media_path.read_bytes())
 return Path(destination),'image/jpeg','photo.jpg'
web._chat_media_cache_path=lambda *args:Path.cwd()/'cache.jpg';web._prune_chat_media_cache=lambda *args:None
web.download_telegram_media_to_path=download
for event_id in (7,8):
 r=client.get('/test-panel/api/users/1/events/'+str(event_id)+'/media')
 assert r.status_code==200,r.text
 assert r.headers['content-type']=='image/jpeg' and r.headers['cache-control']=='private, no-store'
 assert r.content[:2]==b'\xff\xd8'
assert client.get('/test-panel/api/users/2/events/7/media').status_code==404
assert client.get('/test-panel/api/users/1/events/999/media').status_code==404
web.live_users=lambda:([],{});web.user_events.unread_messages_summary=lambda **kw:{'items':[]}
r=client.get('/test-panel/users?registered_from=2026-10-01&registered_to=2026-10-03&page_number=2')
assert r.status_code==200,r.text
assert 'data-filter-registered-from' not in r.text and 'data-filter-registered-to' not in r.text
assert 'По дате регистрации' in r.text
Path('users.html').write_text(r.text)
import nginx_panel_guard as guard
assert 'client_max_body_size 1026m;' in guard.block('/test-panel','/tmp/test.sock')
'''
    env = {**os.environ, 'PYTHONPATH': str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')}
    result = subprocess.run([sys.executable, '-c', script, str(ROOT)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    html = (tmp_path/'users.html').read_text()
    assert 'data-user-filter-form' in html
    assert 'data-filter-status' in html
    assert 'data-page-next' in html
    users_js = (ROOT / "static" / "users.js").read_text(encoding="utf-8")
    assert "data-user-filter-form" in users_js or "user-filter-search" in users_js


def test_home_for_registered_user_without_vpn():
    from services.subscription_status import has_subscription
    ns = functions('main.py', ['send_home'], {
        'Message': object, 'has_subscription': has_subscription,
        'time': types.SimpleNamespace(time=lambda: 1),
        'resolve_user': AsyncMock(return_value={'enable': 1, 'expiry_time': 0}),
        'get_client_from_panel': AsyncMock(return_value=None),
        'get_user_menu_async': AsyncMock(return_value=None),
        'home_text': lambda summary: summary,
    })
    message = types.SimpleNamespace(answer=AsyncMock(), from_user=types.SimpleNamespace(username='alice', full_name='Alice'))
    asyncio.run(ns['send_home'](message, 1))
    assert 'Активной подписки нет' in message.answer.call_args.args[0]
    assert 'бессрочно' not in message.answer.call_args.args[0].lower()
