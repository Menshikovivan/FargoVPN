import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def test_routes_require_auth_csrf_and_explicit_confirmation(tmp_path):
    tree=ast.parse((ROOT/'tests/test_panel_http.py').read_text(encoding='utf8'))
    func=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='test_panel_routes_and_publisher_boundary')
    source=next(x.value.value for x in func.body if isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='script' for t in x.targets))
    setup=source.split('client = TestClient(web.app)',1)[0]
    stub='''\nimport os,sys,types\nif os.name == "nt":\n f=types.ModuleType("fcntl"); f.LOCK_EX,f.LOCK_UN,f.LOCK_SH,f.LOCK_NB=2,8,1,4; f.flock=lambda *a:None;sys.modules['fcntl']=f\n'''
    check=r'''
from fastapi import HTTPException
from pathlib import Path
class Request:
 def __init__(self,auth=True,method='GET'):
  self.method=method;self.headers={};self.session={'auth':True,'auth_version':web.auth_credentials_version(),'user':'admin','csrf_token':'valid'} if auth else {}
def expect(code,fn):
 try:fn()
 except HTTPException as e:assert e.status_code==code;return
 raise AssertionError('expected error '+str(code))
web._expiry_snapshot_dir=lambda: Path('private')
web.fetch_snapshot_sync=lambda **kw: {'stale':False,'clients':[{'uuid':'u','email':'x@example.test','expiry_time':123}]}
web.page=lambda request,title,body,active='':body
expect(401,lambda:web.expiry_snapshots_page(Request(False)))
expect(401,lambda:web.expiry_snapshots_create(Request(False,'POST'),'valid','yes'))
expect(403,lambda:web.expiry_snapshots_create(Request(True,'POST'),'bad','yes'))
expect(400,lambda:web.expiry_snapshots_create(Request(True,'POST'),'valid','no'))
assert not list(Path('private').glob('snapshot-*.json'))
result=web.expiry_snapshots_create(Request(True,'POST'),'valid','yes')
assert result.status_code==303 and len(list(Path('private').glob('snapshot-*.json')))==1
web.fetch_snapshot_sync=lambda **kw: {'stale':False,'clients':[{'uuid':'u','email':'x@example.test','expiry_time':0}]}
html=web.expiry_snapshots_page(Request())
assert 'Стали бессрочными: 1' in html and 'x@example.test' in html
'''
    result=subprocess.run([sys.executable,'-c',stub+setup+check,str(ROOT/'app')],cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(ROOT/'app')},capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
