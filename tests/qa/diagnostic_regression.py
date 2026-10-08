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

import diagnostic_jobs as jobs
jobs.DIRECTORY=pathlib.Path(tempfile.mkdtemp())/'reports'
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
client=TestClient(w.app)
assert client.post('/qa-panel/api/diagnostics/collect').status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'qa','csrf_token':'csrf'}).encode())).decode();client.cookies.set('session',cookie)
assert client.post('/qa-panel/api/diagnostics/collect').status_code==403
r=client.post('/qa-panel/api/diagnostics/collect',headers={'X-CSRF-Token':'csrf'});assert r.status_code==200,r.text;job=r.json()['job']
for _ in range(60):
 r=client.get('/qa-panel/api/diagnostics/collect/'+job);assert r.status_code==200,r.text
 if r.json()['state']!='running':break
 time.sleep(.2)
assert r.json()['state']=='completed',r.text
report=client.get('/qa-panel/api/diagnostics/collect/'+job+'/download');assert report.status_code==200 and 'attachment' in report.headers['content-disposition'];assert 'SUMMARY' in report.text
assert client.get('/qa-panel/api/diagnostics/collect/'+('a'*32)).status_code==404
try:jobs.read(job,'different-owner')
except KeyError:pass
else:raise AssertionError('other owner could read report')
# Explicit errors stay visible through status API.
jobs.save('b'*32,{'owner':'qa','state':'failed','started':time.time(),'error':'test failure'})
assert client.get('/qa-panel/api/diagnostics/collect/'+('b'*32)).json()['error']=='test failure'
# Stateless collector never imports executable config and masks quoted secrets.
import diagnose
secret='secret with spaces'
assert secret not in diagnose.redact('password="'+secret+'"')
f=pathlib.Path(tempfile.mkdtemp());(f/'config.py').write_text('raise RuntimeError("must not execute")\nBOT_TOKEN="synthetic-secret"\n')
assert diagnose.read_config(f)['BOT_TOKEN']=='synthetic-secret'
print('diagnostic actual process/API/auth/CSRF/download/error/owner/redaction PASS')
