"""HTTP authorization and target-ownership checks with synthetic devices."""
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_hwid_http_requires_session_csrf_and_correct_profile(tmp_path):
    tree = ast.parse((ROOT / 'tests/test_panel_http.py').read_text(encoding='utf-8'))
    func = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'test_panel_routes_and_publisher_boundary')
    script = next(x.value.value for x in func.body if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'script' for t in x.targets))
    setup = script.split('client = TestClient(web.app)', 1)[0]
    stub = '''\nimport os, sys, types\nif os.name == "nt":\n fcntl=types.ModuleType("fcntl"); fcntl.LOCK_EX,fcntl.LOCK_UN,fcntl.LOCK_SH,fcntl.LOCK_NB=2,8,1,4; fcntl.flock=lambda *a:None;sys.modules['fcntl']=fcntl\n'''
    check = r'''
from fastapi import HTTPException
config.WEB_USERNAME = 'admin'
class Request:
 def __init__(self,auth=True,csrf=None,method='GET'):
  self.method=method;self.headers={'x-csrf-token':csrf} if csrf else {};self.session={'auth':True,'auth_version':web.auth_credentials_version(),'user':'admin','csrf_token':'valid-csrf'} if auth else {}
calls=[]
web.get_user=lambda tg: {'email':'Profile+42','uuid':'uuid-42'} if tg==42 else None
web.resolve_hwid_client_email_sync=lambda e,u: e if (e,u)==('Profile+42','uuid-42') else (_ for _ in ()).throw(ValueError())
web.list_client_hwids_sync=lambda email: [{'id':11,'deviceModel':'<img src=x onerror=alert(1)>','deviceOs':'Android'}]
web.remove_client_hwid_sync=lambda email,device_id: calls.append((email,device_id))
web.audit=lambda *a,**k:None
def expect(code,fn):
 try: fn()
 except HTTPException as e: assert e.status_code==code;return
 raise AssertionError('expected HTTP '+str(code))
expect(401,lambda:web.user_hwids_api(Request(False),42))
response=web.user_hwids_api(Request(),42);assert response.status_code==200 and json.loads(response.body)['devices'][0]['id']==11
expect(403,lambda:web.user_hwid_remove_api(Request(method='POST'),42,11));assert calls==[]
expect(404,lambda:web.user_hwid_remove_api(Request(csrf='valid-csrf',method='POST'),42,12));assert calls==[]
expect(404,lambda:web.user_hwid_remove_api(Request(csrf='valid-csrf',method='POST'),43,11));assert calls==[]
assert web.user_hwid_remove_api(Request(csrf='valid-csrf',method='POST'),42,11).status_code==200 and calls==[('Profile+42',11)]
web.resolve_hwid_client_email_sync=lambda *a: (_ for _ in ()).throw(ValueError('secret@example.test'))
try: web.user_hwids_api(Request(),42)
except HTTPException as error: assert error.status_code==502 and 'secret@example.test' not in error.detail
else: raise AssertionError('expected failure')
class Db:
 def __enter__(self): return self
 def __exit__(self,*a): pass
 def execute(self,*a): pass
web.database=lambda:Db()
web.change_client_days_sync=lambda *a,**kw:{'expiry_time':1800000000000,'enable':True}
web.fetch_and_sync=lambda **kw:{}
web.user_events.safe_record_event=lambda *a,**kw:None
web.set_flash=lambda *a,**kw:None
expect(403,lambda:web.adjust_days(Request(method='POST'),42,30,return_to='detail',csrf_token=''))
result=web.adjust_days(Request(method='POST'),42,30,return_to='detail',csrf_token='valid-csrf')
assert result.status_code==303 and result.headers['location']=='/users/id/42'
result=web.adjust_days(Request(method='POST'),42,30,return_to='https://outside.test',csrf_token='valid-csrf')
assert result.headers['location']=='/users'
'''
    result = subprocess.run([sys.executable, '-c', stub + setup + check, str(ROOT / 'app')], cwd=tmp_path, env={**os.environ, 'PYTHONPATH':str(ROOT / 'app')}, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
