"""Real PostgreSQL integration tests. Requires QA_DATABASE_URL ending in _qa on loopback."""
import argparse,base64,importlib.util,json,logging,os,pathlib,sys,tempfile,time
from unittest.mock import patch
ROOT=pathlib.Path(sys.argv[1]).resolve();sys.path.insert(0,str(ROOT))
dsn=os.environ['QA_DATABASE_URL']
from urllib.parse import urlsplit
parts=urlsplit(dsn.replace('postgresql+psycopg','postgresql'))
assert parts.hostname in ('127.0.0.1','localhost') and parts.path.endswith('_qa'),'Refuse non-test database'
os.environ['FARGOVPN_DATABASE_URL']=dsn
spec=importlib.util.spec_from_file_location('config',ROOT/'config.example.py');config=importlib.util.module_from_spec(spec);sys.modules['config']=config;spec.loader.exec_module(config)
config.BOT_TOKEN='';config.DB_PATH='qa';config.DATABASE_URL=dsn;config.WEB_PUBLIC_PREFIX='/qa-panel';config.WEB_USERNAME='qa';config.WEB_PASSWORD_HASH='test-only';config.PUSH_VAPID_PRIVATE_KEY_PATH=str(pathlib.Path(tempfile.mkdtemp())/'vapid.pem');config.PUSH_VAPID_SUBJECT='https://panel.example.test'
config.__file__=str(pathlib.Path(config.PUSH_VAPID_PRIVATE_KEY_PATH).parent/'config.py');pathlib.Path(config.__file__).write_text('PUSH_VAPID_PUBLIC_KEY=""\n')
import psutil
psutil.net_io_counters=lambda:type('Counters',(),{'bytes_recv':0,'bytes_sent':0})()
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
import init_db,db,push_service as push
init_db.migrate()
from seed_regression import NAMES
with db.connect('qa') as c:
 for i in range(1000):
  c.execute('INSERT INTO users(tg_id,username,display_name,enable,expiry_time,registered_at) VALUES(?,?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username',(900000+i,NAMES[i%8]+' '+str(i),NAMES[i%8],int(i%3!=2),1800000000000 if i%3==0 else 1700000000000,'2026-01-01'))
 c.commit();assert c.execute('SELECT count(*) FROM users WHERE tg_id>=900000').fetchone()[0]==1000
results=[]
def test(name,fn):
 try:fn();results.append({'test':name,'result':'PASS'})
 except Exception as e:results.append({'test':name,'result':'FAIL','detail':type(e).__name__+': '+str(e)[:200]})
def sql_search():
 with db.connect('qa') as c:
  assert c.execute('SELECT count(*) FROM users WHERE lower(username) LIKE lower(?)',('%алексей%',)).fetchone()[0]==125
  assert c.execute('SELECT count(*) FROM users WHERE enable=0 AND tg_id>=900000').fetchone()[0]==333
  assert c.execute('SELECT count(*) FROM users WHERE lower(username) LIKE lower(?)',('%NO_SUCH_QA%',)).fetchone()[0]==0
test('seed 1000 / SQL unicode search and status',sql_search)
# Keep private keys/configuration in a test directory only.
push._repair_config_public=lambda public:setattr(config,'PUSH_VAPID_PUBLIC_KEY',public)
pub=push.ensure_vapid_keys();test('VAPID keypair health',lambda:(_ for _ in ()).throw(AssertionError()) if not push._vapid_keypair_health()[0] else None)
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
clientkey=ec.generate_private_key(ec.SECP256R1());keys={'p256dh':push.b64u(clientkey.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)),'auth':push.b64u(b'a'*16)}
push._validate_push_endpoint=lambda endpoint:None
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
client=TestClient(w.app)
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'qa','csrf_token':'csrf'}).encode())).decode();client.cookies.set('session',cookie)
headers={'X-CSRF-Token':'csrf'}
if hasattr(w,'diagnostic_read_only_api'):
 def readonly():
  def fetch(query):
   r=client.get('/qa-panel/api/diagnostics/read-only'+query);assert r.status_code==200,r.text;return r.json()
  with patch.object(w,'migrate_database',side_effect=AssertionError('migration during read')):
   data=fetch('');assert data['read_only'] and data['count']==1000 and data['items']
   assert fetch('?q=Alice%20Exact')['count']==125
   assert fetch('?q=%D0%B0%D0%BB%D0%B5%D0%BA%D1%81%D0%B5%D0%B9')['count']==125
   assert fetch('?status=blocked')['count']==333
   assert fetch('?status=active')['count']==334
   assert fetch('?status=expired')['count']==333
   assert fetch('?q=%25')['count']==0
  assert client.get('/qa-panel/api/diagnostics/read-only?status=unknown').status_code==400
  with db.connect('qa') as c:assert c.execute('SELECT count(*) FROM users').fetchone()[0]==1000
 test('read-only diagnostic API formats/search/all filters/no mutations',readonly)

def subscribe():
 r=client.post('/qa-panel/api/panel/push/subscribe',headers=headers,json={'subscription':{'endpoint':'https://fcm.googleapis.com/fcm/send/qa','keys':keys}})
 assert r.status_code==200,r.text
 with db.connect('qa') as c:assert c.execute('SELECT count(*) FROM panel_push_subscriptions WHERE username=?',('qa',)).fetchone()[0]==1
test('HTTP subscribe writes PostgreSQL',subscribe)
def provider_201():
 class Response:
  is_success=True;status_code=201;text='';content=b''
 sent=[]
 def post(endpoint,**kw):sent.append(kw);return Response()
 with patch.object(push.httpx,'post',post):r=push.notify_panel('qa','qa','QA','test')
 assert r['sent']==1 and len(sent)==1,r
 assert sent[0]['headers']['Content-Encoding']=='aes128gcm' and len(sent[0]['content'])>100
 with db.connect('qa') as c:assert c.execute('SELECT last_success_at FROM panel_push_subscriptions WHERE username=?',('qa',)).fetchone()[0]
test('server encryption/VAPID send HTTP201 (provider fixture)',provider_201)
for status in (410,404):
 def obsolete(status=status):
  subscribe()
  with patch.object(push,'_send_push',lambda *a:(False,status,'gone')):r=push.notify_panel('qa','qa','QA','test')
  assert r['removed']==1,r
  with db.connect('qa') as c:assert c.execute('SELECT count(*) FROM panel_push_subscriptions WHERE username=?',('qa',)).fetchone()[0]==0
 test('obsolete '+str(status)+' removes PostgreSQL subscription',obsolete)
def unsubscribe():
 subscribe();r=client.post('/qa-panel/api/panel/push/unsubscribe',headers=headers,json={'endpoint':'https://fcm.googleapis.com/fcm/send/qa'});assert r.status_code==200,r.text
 with db.connect('qa') as c:assert c.execute('SELECT count(*) FROM panel_push_subscriptions WHERE username=?',('qa',)).fetchone()[0]==0
test('HTTP unsubscribe writes PostgreSQL',unsubscribe)
def log():
 r=client.get('/qa-panel/api/panel/push/logs');assert r.status_code==200 and r.json()['logs']
test('HTTP push journal real PostgreSQL',log)
def csrf():
 assert client.post('/qa-panel/api/panel/push/test').status_code==403
 anon=TestClient(w.app);assert anon.get('/qa-panel/api/panel/push/logs').status_code==401
test('auth and CSRF rejection',csrf)
def stress():
 from concurrent.futures import ThreadPoolExecutor
 def query(i):
  with db.connect('qa') as c:return c.execute('SELECT count(*) FROM users WHERE lower(username) LIKE lower(?)',('%'+NAMES[i%8]+'%',)).fetchone()[0]
 start=time.monotonic()
 with ThreadPoolExecutor(max_workers=12) as pool:assert all(x>0 for x in pool.map(query,range(300)))
 results.append({'test':'300 concurrent DB searches time','result':'PASS','seconds':round(time.monotonic()-start,3)})
test('300 searches / 12 workers',stress)
pathlib.Path(sys.argv[2]).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
print(ROOT.name,'PG PASS',sum(r['result']=='PASS' for r in results),'FAIL',sum(r['result']=='FAIL' for r in results))
for r in results:
 if r['result']=='FAIL':print(r)
# Cleanup only synthetic range and QA-owned subscriptions/logs.
with db.connect('qa') as c:
 c.execute('DELETE FROM users WHERE tg_id>=900000');c.execute('DELETE FROM panel_push_subscriptions WHERE username=?',('qa',));c.execute('DELETE FROM panel_push_logs WHERE username=?',('qa',));c.commit()

if any(t["result"]=="FAIL" for t in results):sys.exit(1)
