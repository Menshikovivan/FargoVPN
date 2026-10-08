"""Regression checks for 5.0.2; external APIs are isolated, never production mutations."""
import os
import subprocess
import sys
from pathlib import Path
import pytest
from test_release_501 import case
ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('legacy,bearer', [(False,False),(True,False),(False,True)])
def test_cookie_auth_csrf_and_web_base_path(tmp_path,legacy,bearer):
    case(tmp_path, f'legacy={legacy!r};bearer={bearer!r}\n'+r'''
import httpx
import services.xui_api as x
config.MASTER_API_TOKEN='rejected' if bearer else ''
config.XUI_USERNAME='operator';config.XUI_PASSWORD='test-password'
x._base_url=lambda:'https://xui.example.test/hidden'
x._HTTP_CLIENTS.session_signature=None
calls=[]
def handle(request):
    calls.append((request.method,request.url.path))
    path=request.url.path
    if path=='/hidden/csrf-token':return httpx.Response(404 if legacy else 200,json={'success':True,'obj':'csrf-token'})
    if path=='/hidden/login':
        assert request.method=='POST'
        if not legacy:assert request.headers['X-CSRF-Token']=='csrf-token'
        assert b'username=operator' in request.content
        return httpx.Response(200,json={'success':True},headers={'set-cookie':'session=abc; Path=/hidden/'})
    if request.headers.get('Authorization'):return httpx.Response(401,json={'success':False})
    assert 'session=abc' in request.headers.get('cookie','')
    if not legacy:assert request.headers['X-CSRF-Token']=='csrf-token'
    if legacy and path!='/hidden/server/status':return httpx.Response(404,json={'success':False})
    return httpx.Response(200,json={'success':True,'obj':{'cpu':42}})
client=httpx.Client(transport=httpx.MockTransport(handle));x._http_client=lambda:client
result=x.request_json_sync('GET','panel/api/server/status')
assert result['obj']['cpu']==42
assert sum(path=='/hidden/login' for _,path in calls)==1
x.request_json_sync('GET','panel/api/server/status')
assert sum(path=='/hidden/login' for _,path in calls)==1
''')

def test_legacy_clients_read_from_inbounds(tmp_path):
    case(tmp_path,r'''
import httpx,json
import services.xui_api as x
paths=[]
def request(method,path,**kw):
    paths.append(path)
    if path.endswith('clients/list'):
        req=httpx.Request(method,'https://test');raise httpx.HTTPStatusError('not found',request=req,response=httpx.Response(404,request=req))
    return {'success':True,'obj':[{'id':3,'settings':json.dumps({'clients':[{'id':'uuid-1','email':'alice','enable':True}]}),'clientStats':[{'email':'alice','up':100,'down':200}]}]}
x.request_json_sync=request
result=x._client_list_sync()['obj'][0]
assert result['uuid']=='uuid-1' and result['up']==100 and result['inboundId']==3
''')

def test_settings_and_vapid_writes_do_not_lose_values(tmp_path):
    case(tmp_path,r'''
from concurrent.futures import ThreadPoolExecutor
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
import push_service
w.CONFIG_PATH=Path('config.py');w.CONFIG_PATH.write_text('SERVICE_NAME="before"\nREMINDER_DAYS=[7]\nPUSH_VAPID_PUBLIC_KEY="before"\n')
w.invalidate_snapshot_cache=lambda:None;w.update_manager.invalidate_update_cache=lambda:None
with ThreadPoolExecutor(max_workers=4) as pool:
    jobs=[pool.submit(w.save_config_values,{'REMINDER_DAYS':[9,4,1]}) if i%2 else pool.submit(push_service._repair_config_public,'after') for i in range(20)]
    for job in jobs:job.result()
ns={};exec(w.CONFIG_PATH.read_text(),ns)
assert ns['REMINDER_DAYS']==[9,4,1] and ns['PUSH_VAPID_PUBLIC_KEY']=='after'
assert w.CONFIG_PATH.stat().st_mode&0o777==0o600
''')

def test_backup_two_parts_rejoin_exactly(tmp_path):
    case(tmp_path,r'''
import backup
source=Path('backup.tar.gz');source.write_bytes(bytes(range(256))*4)
assert backup.split_for_telegram(source,Path('single'),2048)==[source]
backup.BACKUP_SINGLE_FILE_BYTES=300
parts=backup.split_for_telegram(source,Path('parts'),2048)
assert len(parts)==2 and b''.join(p.read_bytes() for p in parts)==source.read_bytes()
assert all(p.stat().st_mode&0o777==0o600 for p in parts)
large=backup.split_for_telegram(source,Path('large'),200)
assert all(p.stat().st_size<=200 for p in large)
assert b''.join(p.read_bytes() for p in large)==source.read_bytes()
''')

def test_panel_dom_runtime(tmp_path):
    if not os.getenv('FARGOVPN_JSDOM_PATH'):
        pytest.skip('Set FARGOVPN_JSDOM_PATH to installed jsdom for DOM runtime checks')
    env={**os.environ,'PYTHONPATH':str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')}
    rendered=tmp_path/'rendered'
    render=subprocess.run([sys.executable,str(ROOT/'tests/qa/render_panel.py'),str(ROOT),str(rendered)],env=env,capture_output=True,text=True,timeout=30)
    assert render.returncode==0,render.stderr
    result=subprocess.run(['node',str(ROOT/'tests/qa/panel_dom.cjs'),str(ROOT),str(rendered)],env=env,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr

def test_empty_status_is_error_not_fake_zero(tmp_path):
    case(tmp_path,r'''
import services.xui_api as x
x.request_json_sync=lambda *a,**kw:{'success':True,'obj':None}
snapshot=x.fetch_control_snapshot_sync(force=True)
assert snapshot['status']=={} and 'пустой server/status' in snapshot['error']
''')

def test_read_only_nginx_and_disabled_main_sync(tmp_path):
    case(tmp_path,r'''
import update_manager as m
config.GITHUB_MAIN_SYNC_ENABLED=False
try:m._github_main_sync(Path('unused'),'5.0.2','unused')
except m.UpdateError as e:assert 'GITHUB_MAIN_SYNC_ENABLED' in str(e)
else:raise AssertionError('Disabled sync reported success')
s=(root/'install.sh').read_text();a=s.index('setup_existing_nginx_route() {');b=s.index('\n}\n',a)
assert '--once' not in s[a:b] and 'nginx -t' in s[a:b]
assert 'systemctl disable --now vpn-service-nginx-guard.service' in s
''')

def test_log_credentials_and_tracebacks_are_redacted(tmp_path):
    case(tmp_path,r'''
import io
from log_security import install_log_redaction
config.BOT_TOKEN='12345:test-secret';config.GITHUB_API_TOKEN='github-test-secret'
stream=io.StringIO();handler=logging.StreamHandler(stream);logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO);install_log_redaction()
logging.getLogger('httpx').info('HTTP Request: %s', 'https://api.telegram.org/bot12345:test-secret/sendMessage')
try:raise RuntimeError('token=github-test-secret')
except RuntimeError:logging.getLogger('web').exception('Send failed')
text=stream.getvalue()
assert 'test-secret' not in text and 'github-test-secret' not in text
assert '[REDACTED]' in text and 'Traceback' in text
''')
