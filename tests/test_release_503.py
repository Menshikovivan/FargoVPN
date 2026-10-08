"""5.0.3 failure, integration-boundary and bounded-load regression checks."""
from pathlib import Path
from test_release_501 import case


def test_tail_large_file_and_long_line_memory_bound(tmp_path):
    case(tmp_path,r'''
import tracemalloc,time
from log_reader import tail_lines
p=Path('large.log')
with p.open('wb') as f:
    f.seek(512*1024*1024);f.write(b'\n'+b''.join(('line %d\n'%i).encode() for i in range(1500)))
tracemalloc.start();started=time.monotonic()
for count in (100,500,1000):
    lines,truncated=tail_lines(p,count)
    assert len(lines)==count and lines[-1]=='line 1499\n' and not truncated
assert tracemalloc.get_traced_memory()[1]<8*1024*1024
assert time.monotonic()-started<3
p.write_bytes(b'x'*(3*1024*1024))
lines,truncated=tail_lines(p,100)
assert truncated and len(lines)==1
p.write_bytes('Привет\nмир\n'.encode());assert tail_lines(p,1)[0]==['мир\n']
p.write_bytes(b'a\nb');assert tail_lines(p,2)[0]==['a\n','b']
''')


def test_app_log_http_auth_limits_and_errors(tmp_path):
    case(tmp_path,r'''
import base64,json
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
c=TestClient(w.app);url='/qa-panel/api/panel/app-log'
assert c.get(url).status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();c.cookies.set('session',cookie)
p=Path('bot.log');w._app_log_path=lambda:p
assert c.get(url).status_code==404
p.write_text('one\ntwo\n')
assert c.get(url+'?limit=1000').json()['lines']==['one\n','two\n']
import log_reader
with patch.object(log_reader,'tail_lines',side_effect=PermissionError()):
    response=c.get(url);assert response.status_code==403 and 'Нет прав' in response.json()['detail']
w._app_log_path=lambda: (_ for _ in ()).throw(w.HTTPException(500,'APP_LOG_PATH отклонён'))
assert c.get(url+'?path=/etc/passwd').status_code==500
''')


def test_push_journal_empty_db_failure_and_migration_concurrency(tmp_path):
    case(tmp_path,r'''
import base64,json,time,threading
from concurrent.futures import ThreadPoolExecutor
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
c=TestClient(w.app);url='/qa-panel/api/panel/push/logs'
assert c.get(url).status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();c.cookies.set('session',cookie)
w.push_service.panel_logs=lambda *a:[]
assert c.get(url).json()=={'ok':True,'logs':[],'empty':True}
w.push_service.panel_logs=lambda *a:(_ for _ in ()).throw(RuntimeError('password=hidden'))
r=c.get(url);assert r.status_code==503 and 'RuntimeError' in r.json()['detail'] and 'hidden' not in r.text
import push_service as p
calls=[];p._migrated_paths.clear()
def migrate():calls.append(1);time.sleep(.01)
p.migrate_database=migrate
with ThreadPoolExecutor(max_workers=20) as pool:list(pool.map(lambda _:p.migrate_tables('same-db'),range(200)))
assert len(calls)==1
p._migrated_paths.clear();p.migrate_database=lambda:(_ for _ in ()).throw(RuntimeError('db down'))
for _ in range(2):
    try:p.migrate_tables('same-db')
    except RuntimeError:pass
    else:raise AssertionError('failed migration cached')
assert not p._migrated_paths
''')


def test_update_status_live_output_restart_and_concurrency(tmp_path):
    case(tmp_path,r'''
import base64,json
from concurrent.futures import ThreadPoolExecutor
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
config.UPDATE_DIR=str(Path('updates').resolve());p=w.update_manager.update_log_path();p.parent.mkdir(parents=True);p.write_text('stage files\nstage database\n')
w.update_manager.write_status('installing',job_id='test-job',progress=74,phase='files',message='files')
c=TestClient(w.app)
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();c.cookies.set('session',cookie)
def query(_):
    r=c.get('/qa-panel/api/updates/status');assert r.status_code==200
    d=r.json();assert d['progress']==74 and 'stage database' in d['output'];return d
with ThreadPoolExecutor(max_workers=12) as pool:assert len(list(pool.map(query,range(100))))==100
# State remains readable independently of web process memory.
import update_manager as m
assert m.read_status()['progress']==74
m.write_status('failed',job_id='test-job',exit_code=7,error='installer failed')
d=c.get('/qa-panel/api/updates/status').json();assert d['state']=='failed' and d['exit_code']==7
s=(root/'webapp.py').read_text();start=s.index('function animateProgress(');end=s.index('\nfunction setRequestError',start)
assert 'Math.exp' not in s[start:end] and 'shownProgress=serverProgress' in s[start:end]
''')


def test_github_release_tag_matches_commit_and_assets(tmp_path):
    case(tmp_path,r'''
import httpx,tarfile,hashlib
import update_manager as m
config.UPDATE_DIR=str(Path('updates').resolve());config.GITHUB_TARGET_BRANCH='main'
p=Path('package');p.mkdir();(p/'VERSION').write_text('5.0.3');(p/'install.sh').write_text('#!/bin/bash\n');(p/'CHANGELOG.md').write_text('## 5.0.3\n- fix\n')
a=Path('release.tar.gz')
with tarfile.open(a,'w:gz') as t:t.add(p,arcname='FargoVPN-5.0.3')
commit='c'*40;tags={};calls=[];upload_bad=False
m._github_main_sync=lambda *a,**kw:{'synced':True,'commit_sha':commit,'branch':'main','commit_url':'https://github.com/test/commit/'+commit,'files':[]}
def req(method,path,**kw):
    calls.append((method,path,kw))
    if '/git/ref/tags/' in path:
        tag=path.rsplit('/',1)[-1];return httpx.Response(200,json={'object':{'type':'commit','sha':tags[tag]}}) if tag in tags else httpx.Response(404,json={})
    if path.endswith('/git/refs'):
        tags[kw['json']['ref'].split('/')[-1]]=kw['json']['sha'];return httpx.Response(201,json={})
    if '/releases/tags/' in path:return httpx.Response(404,json={})
    if path.endswith('/releases'):
        assert kw['json']['target_commitish']==commit
        return httpx.Response(201,json={'id':5,'upload_url':'https://uploads.github.com/repos/test/test/releases/5/assets{?name,label}','html_url':'https://github.com/test/releases/tag/5.0.3'})
    raise AssertionError(path)
m.github_request=req;m._asset_upload_allowed=lambda *a:True;m.github_headers=lambda *a,**kw:{}
def upload(url,**kw):
    data=kw['content'];name=kw['params']['name']
    return httpx.Response(201,json={'name':name,'state':'uploaded','size':len(data)+(1 if upload_bad else 0),'digest':'sha256:'+hashlib.sha256(data).hexdigest(),'browser_download_url':'https://github.com/test/'+name})
m.httpx.post=upload
result=m.publish_update(a,a.name)
assert result['github_main_commit_sha']==commit and tags[result['github_tag']]==commit
assert len(result['github_release_assets'])==4
upload_bad=True
try:m.publish_update(a,a.name)
except m.UpdateError as e:assert 'не подтвердил размер' in str(e)
else:raise AssertionError('bad asset accepted')
upload_bad=False
# Never force-overwrite a historical tag; report partial main update precisely.
tags[result['github_tag']]='d'*40
try:m.publish_update(a,a.name)
except m.UpdateError as e:assert 'другой коммит' in str(e)
else:raise AssertionError('tag overwritten')
''')


def test_missing_telemetry_not_presented_as_measurement(tmp_path):
    case(tmp_path,r'''
import services.xui_api as x
s=x._normalize_server_status({'xray':{'state':'running'}})
assert s['_available']=={'cpu':False,'tcp_count':False,'network':False}
s=x._normalize_server_status({'cpu':0,'tcpCount':0,'netIO':{'up':0,'down':0}})
assert all(s['_available'].values())
assert not x._normalize_server_status({'cpu':'NaN'})['_available']['cpu']
''')


def test_parallel_publication_refuses_second_job(tmp_path):
    case(tmp_path,r'''
import threading
from concurrent.futures import ThreadPoolExecutor
import update_manager as m
config.UPDATE_DIR=str(Path('updates').resolve())
entered=threading.Event();finish=threading.Event()
def publish(*a):entered.set();assert finish.wait(3);return {'ok':True}
m._publish_update_locked=publish
with ThreadPoolExecutor(max_workers=2) as pool:
    first=pool.submit(m.publish_update,Path('unused'));assert entered.wait(2)
    try:m.publish_update(Path('unused'))
    except m.UpdateError as e:assert 'уже выполняется' in str(e)
    else:raise AssertionError('duplicate publication queued')
    finish.set();assert first.result()['ok']
''')


def test_users_large_list_dynamic_stress():
    import os
    import subprocess
    import pytest
    if not os.environ.get('FARGOVPN_JSDOM_PATH'):pytest.skip('jsdom required')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/qa/users_stress.cjs'),str(root)],capture_output=True,text=True,timeout=40)
    assert result.returncode==0,result.stdout+result.stderr
    assert '\"users\":5000' in result.stdout


def test_late_push_check_does_not_overwrite_new_action():
    from test_release_501 import node
    root=Path(__file__).resolve().parents[1]
    source=(root/'webapp.py').read_text()
    check=source[source.index('const check=async()=>{'):source.index('\nconst enable=async()=>{')]
    node(r'''
let actionBusy=false,contextGeneration=0,status='',pending;
const setPush=value=>status=value,pushCapability=()=>({supported:false}),window={isSecureContext:true},call=()=>new Promise(resolve=>pending=resolve),renderLogs=async()=>{};
'''+check+r'''
(async()=>{const promise=check();contextGeneration++;status='Включены';pending({ok:true,endpoint_hashes:[]});await promise;if(status!=='Включены')throw Error('Late check overwrote newer action');})().catch(e=>{console.error(e);process.exitCode=1});
''')
