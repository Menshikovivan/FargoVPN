"""Behavior checks for 5.0.1. All external services are isolated or mocked."""
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
HEADER=r'''
import importlib.util,sys,logging
from pathlib import Path
from unittest.mock import patch
root=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('config',root/'config.example.py')
config=importlib.util.module_from_spec(spec);sys.modules['config']=config;spec.loader.exec_module(config)
config.BOT_TOKEN='';config.MASTER_API_TOKEN='test-only';config.WEB_PUBLIC_PREFIX='/qa-panel'
config.BASE_URL='https://xui.example.test/hidden/';config.XUI_INTERNAL_BASE_URL='';config.XUI_MIN_REQUEST_INTERVAL_MS=0;config.DB_PATH='unused'
config.GITHUB_REPOSITORY_OWNER='Menshikovivan';config.GITHUB_REPOSITORY_NAME='FargoVPN'
import psutil
psutil.net_io_counters=lambda:type('Counters',(),{'bytes_recv':0,'bytes_sent':0})()
'''

def case(tmp_path,code):
    r=subprocess.run([sys.executable,'-c',HEADER+code,str(ROOT)],cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')},capture_output=True,text=True,timeout=45)
    assert r.returncode==0,r.stdout+r.stderr

def node(code):
    r=subprocess.run(['node','-e',code],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stdout+r.stderr

def guard():
    s=(ROOT/'install.sh').read_text();a=s.index('if [[ -z "$UPDATE_PATH" && ! -t 0 ]]');return s[a:s.index('\nINSTALL_LOG=',a)]

def test_piped_menu_reads_terminal(tmp_path):
    child=tmp_path/'installer.sh';child.write_text('set -Eeuo pipefail\nUPDATE_PATH=""\n'+guard()+"\nread -rp 'MENU: ' MODE\nprintf 'SELECTED=%s\\n' \"$MODE\"\n")
    pid,fd=pty.fork()
    if pid==0:os.execl('/bin/bash','bash','-c','printf bootstrap-input | bash "$1"','test',str(child))
    output=b'';deadline=time.monotonic()+10
    try:
        while b'MENU:' not in output and time.monotonic()<deadline:
            if select.select([fd],[],[],0.5)[0]:output+=os.read(fd,4096)
        assert b'MENU:' in output,output
        os.write(fd,b'2\n')
        while b'SELECTED=2' not in output and time.monotonic()<deadline:
            if select.select([fd],[],[],0.5)[0]:
                try:output+=os.read(fd,4096)
                except OSError:break
        assert b'SELECTED=2' in output,output
    finally:
        os.close(fd)
        try:os.kill(pid,signal.SIGTERM)
        except ProcessLookupError:pass
        os.waitpid(pid,0)

@pytest.mark.parametrize('path,expected',[('',2),('/root/vpn_bot',0)])
def test_menu_without_tty_and_automatic_update(path,expected):
    r=subprocess.run(['bash','-c',f'set -Eeuo pipefail\nUPDATE_PATH="{path}"\n'+guard()+'\necho AUTO'],input='',text=True,capture_output=True,start_new_session=True)
    assert r.returncode==expected
    if expected:assert 'Нет доступного терминала' in r.stderr and 'AUTO' not in r.stdout
    else:assert r.stdout.strip()=='AUTO'

def test_xui_shapes_and_counter_reset(tmp_path):
    case(tmp_path,r'''
import time,math
from services import xui_api as x
assert x.normalize_client({'email':'a','enable':'false'})['enable'] is False
s=x._normalize_server_status({'netIO':{'up':123,'down':456},'netTraffic':{'sent':10000,'recv':20000},'cpu':'bad','load':{'load1':'NaN'}})
assert (s['net_up'],s['net_down'],s['net_up_speed'],s['net_down_speed'])==(10000,20000,123,456)
assert s['cpu']==0 and s['load1']==0
x._SERVER_TRAFFIC_SAMPLE.update({'ts':time.monotonic()-2,'up':10000,'down':20000})
s=x._normalize_server_status({'netIO':{'up':10,'down':20}});assert s['net_up_speed']==0 and s['net_down_speed']==0
before=dict(x._SERVER_TRAFFIC_SAMPLE);x._normalize_server_status({});assert x._SERVER_TRAFFIC_SAMPLE==before
n=x._normalize_node_summaries([None,{'cpuPct':'bad','memPct':'NaN','enable':'false','panelVersion':'v3','apiToken':'SECRET'}])[0]
assert n['enabled'] is False and n['version']=='v3' and 'apiToken' not in n and math.isfinite(n['mem_pct'])
''')

@pytest.mark.parametrize('failure',['timeout','status'])
def test_no_duplicate_xui_post_after_failure(tmp_path,failure):
    case(tmp_path,r'''
import httpx
from services import xui_api as x
calls=[];x._request_url_candidates=lambda p:['https://test/a','https://test/b']
def transport(request):
    calls.append(request)
    FAILURE
x._http_client=lambda:httpx.Client(transport=httpx.MockTransport(transport))
try:x.request_json_sync('POST','clients/update',{'days':30})
except (httpx.RequestError,httpx.HTTPStatusError):pass
else:raise AssertionError('failure expected')
assert len(calls)==1
assert x._XUI_REQUEST_GATE.acquire(blocking=False);x._XUI_REQUEST_GATE.release()
'''.replace('FAILURE',"raise httpx.ReadTimeout('timeout',request=request)" if failure=='timeout' else "return httpx.Response(503,json={})"))

def test_xui_get_retries_and_fallback(tmp_path):
    case(tmp_path,r'''
import httpx
from services import xui_api as x
calls=[];x.time.sleep=lambda s:None;x._request_url_candidates=lambda p:['https://test/a','https://test/b']
def transport(request):
    calls.append(request);assert request.headers['Authorization']=='Bearer test-only'
    if request.url.path=='/a':return httpx.Response(404,json={})
    if len(calls)==2:return httpx.Response(503,json={})
    return httpx.Response(200,json={'success':True,'obj':{'healthy':True}})
x._http_client=lambda:httpx.Client(transport=httpx.MockTransport(transport))
assert x.request_json_sync('GET','server/status')['obj']['healthy'] and len(calls)==3
''')

def test_telemetry_cache_optional_routes_and_secrets(tmp_path):
    case(tmp_path,r'''
import httpx
from services import xui_api as x
calls=[]
def req(method,path,**kw):
    calls.append(path)
    if path.endswith('/status'):return {'obj':{'xray':{'state':'running'}}}
    if path.endswith('fail2banStatus'):return {'obj':{'installed':True,'enabled':True,'usable':True,'token':'SECRET'}}
    raise httpx.HTTPStatusError('missing',request=httpx.Request('GET','https://test'),response=httpx.Response(404))
x.request_json_sync=req;s=x.fetch_control_snapshot_sync(force=True)
assert s['status']['xray_state']=='running' and not s['error'] and s['fail2ban']['status']=='enabled' and 'token' not in s['fail2ban']
x.fetch_control_snapshot_sync();assert len(calls)==3
''')

def test_snapshot_traffic_online_and_outage_cache(tmp_path):
    case(tmp_path,r'''
from services import xui_api as x
calls=[]
def req(method,path,**kw):
    calls.append(path)
    if path.endswith('clients/list'):return {'obj':[{'email':'a','id':'uuid1','tgId':12}]}
    if path.endswith('inbounds/list'):return {'obj':[{'clientStats':[{'email':'a','up':12,'down':34}],'enable':True}]}
    if path.endswith('server/status'):return {'obj':{'netTraffic':{'sent':100,'recv':200}}}
    if path.endswith('lastOnline'):return {'obj':{'a':1735689600000}}
    if path.endswith('onlines'):return {'obj':['a']}
    raise AssertionError(path)
x.request_json_sync=req;s=x.fetch_snapshot_sync(force=True)
assert not s['stale'] and s['clients'][0]['online'] and s['clients'][0]['down']==34
assert s['traffic_summary']['used']==46 and s['server_traffic_summary']['used']==300
assert calls.count('panel/api/inbounds/list')==1
x.fetch_snapshot_sync();assert len(calls)==5
x.invalidate_snapshot_cache()
def fail(*a,**kw):calls.append('failure');raise RuntimeError('API unavailable')
x.request_json_sync=fail;s=x.fetch_snapshot_sync()
assert s['stale'] and s['clients'][0]['email']=='a' and 'unavailable' in s['error']
x.fetch_snapshot_sync();assert calls.count('failure')==1
''')

def test_github_secrets_and_release_notes(tmp_path):
    case(tmp_path,r'''
import update_manager as m,hashlib
p=Path('public');p.mkdir()
for name in ('VERSION','install.sh','config.example.py','.env.example','README.md','main.py','config.py','config.py.before_web_123','.env.staging','secret.pem','secret.key','prod.dump','prod.db'):(p/name).write_text('5.0.1' if name=='VERSION' else name)
a=Path('package.tar.gz');a.write_bytes(b'archive')
f=m._github_main_public_files(p,a,'5.0.1',hashlib.sha256(a.read_bytes()).hexdigest())
assert set(f)=={'VERSION','install.sh','config.example.py','.env.example','README.md','main.py','FargoVPN_FULL.tar.gz','FargoVPN_FULL.tar.gz.sha256'}
n=m.github_notes((root/'CHANGELOG.md').read_text(),'5.0.1','a'*64,10)
assert '## 5.0.1' in n and '## 5.0.0' not in n and '### Установка' in n
''')

def test_panel_accepts_501_package_from_500(tmp_path):
    case(tmp_path,r'''
import io,tarfile
import update_manager as m
config.UPDATE_DIR=str(Path.cwd()/'updates');m.current_version=lambda:'5.0.0'
a=Path('update.tar.gz')
with tarfile.open(a,'w:gz') as t:
    for name,data in {'VERSION':b'5.0.1\n','install.sh':b'#!/bin/bash\n','main.py':b'','config.example.py':b''}.items():
        i=tarfile.TarInfo('FargoVPN-5.0.1/'+name);i.size=len(data);t.addfile(i,io.BytesIO(data))
f=m.store_manual_update(a);assert f['version']=='5.0.1' and m.is_newer('5.0.1','5.0.0')
''')

def test_push_crypto_roundtrip_and_vapid(tmp_path):
    case(tmp_path,r'''
import json
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import push_service as p
config.PUSH_VAPID_SUBJECT='https://vpn.example.test'
ua=ec.generate_private_key(ec.SECP256R1());auth=b'0123456789abcdef';payload='Проверка'.encode()
b=p._encrypt({'keys':{'p256dh':p.b64u(p._pub(ua)),'auth':p.b64u(auth)}},payload);salt=b[:16];pub=b[21:86]
shared=ua.exchange(ec.ECDH(),ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),pub))
ikm=HKDF(algorithm=hashes.SHA256(),length=32,salt=auth,info=b'WebPush: info\0'+p._pub(ua)+pub).derive(shared)
cek=HKDF(algorithm=hashes.SHA256(),length=16,salt=salt,info=b'Content-Encoding: aes128gcm\0').derive(ikm)
nonce=HKDF(algorithm=hashes.SHA256(),length=12,salt=salt,info=b'Content-Encoding: nonce\0').derive(ikm)
assert AESGCM(cek).decrypt(nonce,b[86:],None)==payload+b'\x02'
k=ec.generate_private_key(ec.SECP256R1());h,c,s=p._jwt(k,'https://push.test').split('.');raw=p.b64ud(s)
assert json.loads(p.b64ud(c))['aud']=='https://push.test'
k.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32],'big'),int.from_bytes(raw[32:],'big')),(h+'.'+c).encode(),ec.ECDSA(hashes.SHA256()))
''')

def test_push_http_auth_csrf_malformed_and_disabled(tmp_path):
    case(tmp_path,r'''
import base64,json
from itsdangerous import TimestampSigner
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
from fastapi.testclient import TestClient
c=TestClient(w.app);url='/qa-panel/api/panel/push/subscribe'
assert c.post(url,json={}).status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();c.cookies.set('session',cookie)
assert c.post(url,json={}).status_code==403
headers={'X-CSRF-Token':'csrf'}
for payload in ([],None,{'subscription':[]}):assert c.post(url,json=payload,headers=headers).status_code==400
calls=[];w.push_service.panel_subscribe=lambda *a:calls.append(a)
assert c.post(url,json={'subscription':{}},headers=headers).status_code==200 and calls[0][1]=='admin'
config.PUSH_ENABLED=False
assert c.post(url,json={'subscription':{}},headers=headers).status_code==410
assert c.post('/qa-panel/api/panel/push/test',headers=headers).status_code==410
''')

def test_push_buttons_permission_failure_disable_reenable():
    s=(ROOT/'webapp.py').read_text();enable=s[s.index('const enable=async()=>{'):s.index('\nconst disable=async()=>{')];disable=s[s.index('const disable=async()=>{'):s.index('\nconst test=async()=>{')]
    node(r'''
const assert=require('node:assert/strict');let contextGeneration=0,actionBusy=false,prepared=0,subscribed=0,removed=0,status,fail=false;
const b={disabled:false},document={getElementById:()=>b},window={isSecureContext:true,__fargovpnPushContextReady:false,__fargovpnPushContextValue:null};
const events=[],location={origin:'https://test'},navigator={userAgent:'QA',serviceWorker:{ready:Promise.resolve({pushManager:{getSubscription:async()=>sub}})}};
const Notification={permission:'default',requestPermission(){events.push('permission');this.permission='granted';return Promise.resolve('granted')}};
const sub={endpoint:'test',toJSON:()=>({endpoint:'test',keys:{p256dh:'key',auth:'auth'}}),unsubscribe:async()=>{removed++;return true}};
const addClientLog=()=>{},setPush=(...a)=>status=a,renderLogs=async()=>{},pushCapability=()=>({supported:true,ios:false}),isFirefox=()=>false,isChromium=()=>true,isPushRetrievalError=()=>false;
const preparePushContext=async()=>{events.push('prepare');prepared++;return {registration:{},keyBytes:{},keyBuffer:{}}},prepareExistingSubscription=async()=>({subscription:null}),subscribeFromGesture=()=>{subscribed++;return Promise.resolve(sub)},call=async()=>{if(fail)throw new Error('server failure');return {ok:true}};
'''+enable+disable+r'''
(async()=>{
await enable();assert.deepEqual(events,['permission','prepare']);assert.equal(b.disabled,false);
await disable();assert.equal(removed,1);assert.equal(window.__fargovpnPushContextReady,false);assert.equal(window.__fargovpnPushContextValue,null);
await enable();assert.equal(subscribed,2);assert.equal(prepared,2);
fail=true;await enable();assert.equal(b.disabled,false);assert.equal(actionBusy,false);assert.equal(status[0],'Ошибка');
})().catch(e=>{console.error(e);process.exit(1)});
''')

def test_service_worker_cache_and_click_scope():
    s=(ROOT/'service-worker.js').read_text().replace('__FARGOVPN_CACHE__','fargovpn-static-v5.0.1').replace('__FARGOVPN_BASE__','/qa-panel/').replace('__FARGOVPN_VERSION__','5.0.1')
    node(r'''
const assert=require('node:assert/strict'),handlers={},deleted=[],opened=[];
const caches={keys:async()=>['other-app','fargovpn-static-v5.0.0','fargovpn-static-v5.0.1'],delete:async k=>deleted.push(k)};
const self={location:{origin:'https://test',href:'https://test/qa-panel/service-worker.js'},addEventListener:(k,f)=>handlers[k]=f,clients:{claim:async()=>{},matchAll:async()=>[],openWindow:async u=>opened.push(u)}};
'''+s+r'''
(async()=>{let p;handlers.activate({waitUntil:v=>p=v});await p;assert.deepEqual(deleted,['fargovpn-static-v5.0.0']);handlers.notificationclick({notification:{close(){},data:{url:'https://evil.test/'}},waitUntil:v=>p=v});await p;assert.deepEqual(opened,['https://test/qa-panel/cabinet']);})().catch(e=>{console.error(e);process.exit(1)});
''')

def test_live_monitoring_dom_and_safe_node_text():
    node(r'''
const assert=require('node:assert/strict'),elements={},paths=[];
const el=id=>elements[id]||(elements[id]={textContent:'',children:[],replaceChildren(){this.children=[]},appendChild(c){this.children.push(c)}});
const document={hidden:false,querySelector:()=>({content:'/qa-panel/'}),getElementById:el,createElement:()=>({textContent:'',children:[],appendChild(c){this.children.push(c)}})},window={addEventListener(){}};
let scheduled;const setTimeout=f=>{scheduled=f;return 1},clearTimeout=()=>{};
const fetch=async path=>{paths.push(path);return {ok:true,json:async()=>path.endsWith('metrics')&&!path.includes('online')?{cpu:25,ram:50}:path.includes('online')?{count:2}:{status:{xray_state:'running',xray_version:'v1'},fail2ban:{status:'enabled'},nodes:[{name:'<script>bad</script>',status:'online'}],error:''}}};
'''+(ROOT/'static/monitoring.js').read_text()+r'''
setImmediate(()=>{try{assert.equal(paths.length,3);assert(paths.every(p=>p.startsWith('/qa-panel/api/')));assert.equal(el('mon-cpu').textContent,'25%');assert.equal(el('mon-online').textContent,'2');assert.equal(el('mon-xui-fail2ban').textContent,'enabled');assert.equal(el('mon-xui-nodes').children[0].children[0].textContent,'<script>bad</script>');assert.equal(typeof scheduled,'function')}catch(e){console.error(e);process.exit(1)}});
''')

def test_db_empty_batch_and_prefetched_returning_row(tmp_path):
    case(tmp_path,r'''
import db
class NoConnection:
    def execute(self,*a):raise AssertionError('Empty batch must not execute SQL')
assert db.ConnectionWrapper(NoConnection()).executemany('INSERT INTO users(tg_id) VALUES(?)',[]).rowcount==0
class Result:
    def fetchall(self):return [(2,)]
    def fetchmany(self,n):return [(2,)][:n]
    def __iter__(self):return iter([(2,)])
for method in ('fetchall','fetchmany','iter'):
    r=db.CompatResult(Result(),prefetched=(1,))
    rows=r.fetchall() if method=='fetchall' else r.fetchmany(2) if method=='fetchmany' else list(r)
    assert [row[0] for row in rows]==[1,2] and r.lastrowid==1
''')

@pytest.mark.parametrize('action,expected',[("on_signal INT",130),("on_signal TERM",143),("on_signal HUP",129),("bash -c 'exit 7'",7)])
def test_installer_failure_and_signal_status(action,expected):
    s=(ROOT/'install.sh').read_text();a=s.index('on_error() {');b=s.index('\ntrap cleanup EXIT',a)
    setup='set -Eeuo pipefail\nrestart_existing_services(){ :; };summary_fail(){ :; };write_update_status(){ :; };print_update_summary(){ :; };\n'
    r=subprocess.run(['bash','-c',setup+s[a:b]+'\ntrap \'on_error "$LINENO" "$BASH_COMMAND" "$?"\' ERR\n'+action],capture_output=True,text=True)
    assert r.returncode==expected,r.stderr

@pytest.mark.skipif(os.geteuid()!=0,reason='Public bootstrap requires root')
@pytest.mark.parametrize('corrupt',[False,True])
def test_public_bootstrap_checksum_cleanup_and_exit_status(tmp_path,corrupt):
    case(tmp_path,r'''
import tarfile,io,hashlib,os,subprocess
import update_manager as m
archive=Path('FULL.tar.gz')
with tarfile.open(archive,'w:gz') as t:
    for name,data in {'VERSION':b'5.0.1\n','install.sh':b'#!/bin/bash\necho CHILD-RAN\nexit 7\n'}.items():
        i=tarfile.TarInfo('FargoVPN-5.0.1/'+name);i.size=len(data);t.addfile(i,io.BytesIO(data))
sumfile=Path('FULL.sha256');sumfile.write_text(('0'*64 if CORRUPT else hashlib.sha256(archive.read_bytes()).hexdigest())+'  FargoVPN_FULL.tar.gz\n')
b=Path('bootstrap.sh');b.write_text(m._github_main_bootstrap())
temp=Path('bootstrap-temp');temp.mkdir()
env={**os.environ,'FARGOVPN_ARCHIVE_URL':archive.resolve().as_uri(),'FARGOVPN_CHECKSUM_URL':sumfile.resolve().as_uri(),'FARGOVPN_BOOTSTRAP_TMPDIR':str(temp.resolve())}
r=subprocess.run(['bash',str(b),'--update-existing','/root/vpn_bot'],stdin=subprocess.DEVNULL,capture_output=True,text=True,env=env,timeout=15)
assert not list(temp.iterdir()),r.stdout+r.stderr
if CORRUPT:assert r.returncode!=0 and 'CHILD-RAN' not in r.stdout
else:assert r.returncode==7 and 'CHILD-RAN' in r.stdout
'''.replace('CORRUPT',str(corrupt)))

def test_monitoring_settings_and_rendered_javascript(tmp_path):
    case(tmp_path,r'''
import base64,json,re,subprocess
from itsdangerous import TimestampSigner
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
from fastapi.testclient import TestClient
w.inbound_selection_status_sync=lambda **kw:{'ok':True,'items':[],'selected':[],'missing':[],'error':''}
w.service_state=lambda *a:'active';w.update_manager.cached_update_info=lambda:{}
w.fetch_control_snapshot_sync=lambda:{'ts':1,'status':{'xray_state':'running','xray_version':'v1'},'nodes':[],'fail2ban':{'status':'enabled'},'error':''}
w.psutil.process_iter=lambda *a:[]
from types import SimpleNamespace
w.psutil.virtual_memory=lambda:SimpleNamespace(percent=25,used=1024,total=4096)
w.psutil.swap_memory=lambda:SimpleNamespace(percent=0,used=0,total=1024)
w.psutil.disk_usage=lambda *a:SimpleNamespace(percent=10,free=1000000)
w.psutil.cpu_percent=lambda *a:12
w.psutil.boot_time=lambda:1700000000
w.psutil.getloadavg=lambda:(0.1,0.2,0.3)
c=TestClient(w.app)
assert c.get('/qa-panel/api/xui-telemetry').status_code==401
cookie=TimestampSigner(config.WEB_SECRET_KEY).sign(base64.b64encode(json.dumps({'auth':True,'user':'admin','csrf_token':'csrf'}).encode())).decode();c.cookies.set('session',cookie)
for page in ('settings?tab=notifications','monitoring'):
    r=c.get('/qa-panel/'+page);assert r.status_code==200,r.text
    for index,script in enumerate(re.findall(r'<script\b[^>]*>(.*?)</script>',r.text,re.S)):
        f=Path('inline_'+page.split('?')[0]+str(index)+'.js');f.write_text(script)
        check=subprocess.run(['node','--check',str(f)],capture_output=True,text=True);assert check.returncode==0,check.stderr
    if page=='monitoring':assert '/qa-panel/static/monitoring.js' in r.text and 'id="mon-xui-nodes-card"' in r.text
    else:assert 'panel-push-enable' in r.text and 'data-tab="notifications"' in r.text
r=c.get('/qa-panel/api/xui-telemetry');assert r.status_code==200 and 'no-store' in r.headers['cache-control']
assert r.json()['status']['xray_state']=='running'
''')

@pytest.mark.parametrize('raced',[False,True])
def test_github_main_atomic_commit_and_modes(tmp_path,raced):
    case(tmp_path,r'''
import io,tarfile,httpx
import update_manager as m
archive=Path('release.tar.gz')
with tarfile.open(archive,'w:gz') as t:
    for name,data in {'VERSION':b'5.0.1\n','install.sh':b'#!/bin/bash\n','scripts/healthcheck.sh':b'#!/bin/bash\n','main.py':b''}.items():
        i=tarfile.TarInfo('FargoVPN-5.0.1/'+name);i.size=len(data);t.addfile(i,io.BytesIO(data))
refs=0;calls=[];tree=[]
def req(method,path,**kw):
    global refs,tree
    calls.append((method,path,kw))
    if method=='GET' and '/git/ref/' in path:
        refs+=1;return httpx.Response(200,json={'object':{'sha':('b' if RACED and refs==2 else 'a')*40}})
    if path.endswith('/git/blobs'):return httpx.Response(201,json={'sha':'c'*40})
    if path.endswith('/git/trees'):
        tree=kw['json']['tree'];return httpx.Response(201,json={'sha':'d'*40})
    if path.endswith('/git/commits'):
        assert kw['json']['message']=='Release 5.0.1'
        return httpx.Response(201,json={'sha':'e'*40,'html_url':'https://github.test/commit'})
    if method=='PATCH':return httpx.Response(200,json={'object':{'sha':'e'*40}})
    raise AssertionError(path)
m.github_request=req
try:
    result=m._github_main_sync(archive,'5.0.1',m.sha256_file(archive))
    assert not RACED and result['synced']
except m.UpdateError as exc:
    assert RACED and 'изменилась' in str(exc)
patches=[kw for method,path,kw in calls if method=='PATCH']
assert len(patches)==(0 if RACED else 1)
if patches:assert patches[0]['json']['force'] is False
modes={i['path']:i['mode'] for i in tree}
assert modes['install.sh']=='100755' and modes['scripts/healthcheck.sh']=='100755' and modes['main.py']=='100644'
'''.replace('RACED',str(raced)))

def test_concurrent_vapid_initialization_keeps_one_private_key(tmp_path):
    case(tmp_path,r'''
from concurrent.futures import ThreadPoolExecutor
import push_service as p,stat
config.PUSH_VAPID_PRIVATE_KEY_PATH=str(Path('vapid.pem').resolve())
# Suppress production config persistence; this test only owns a temporary key.
p._repair_config_public=lambda public:setattr(config,'PUSH_VAPID_PUBLIC_KEY',public)
with patch.dict(sys.modules,{'webapp':None}):
    with ThreadPoolExecutor(max_workers=8) as pool:keys=list(pool.map(lambda _:p.ensure_vapid_keys(),range(16)))
assert len(set(keys))==1 and p.public_key()==keys[0]
assert stat.S_IMODE(Path('vapid.pem').stat().st_mode)==0o600
assert not list(Path('.').glob('vapid.pem.*')) or [i.name for i in Path('.').glob('vapid.pem.*')]==['vapid.pem.lock']
''')
