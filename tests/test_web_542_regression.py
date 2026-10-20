"""Targeted original/candidate regression; no real services or user data."""
import ast,html,json,types
from pathlib import Path
BASE=Path('work/audit-5.1.41')
def load(path):
 s=path.read_text(encoding='utf-8');return s,ast.parse(s)
class HTTPException(Exception):
 def __init__(self,status,detail):self.status_code=status
class Conn:
 def __init__(self,value):self.value=value;self.updated=False
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def execute(self,sql,args):
  if sql.startswith('UPDATE'):self.updated=True
  return self
 def fetchone(self):return (self.value,)
def probe_web(path):
 s,t=load(path);out={}
 funcs={n.name:n for n in t.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
 for value,key in [(0,'existing_zero'),(None,'new_zero'),(200,'nonzero_to_zero')]:
  c=Conn(value);ns={'Request':object,'Form':lambda x:x,'require_auth':lambda r:None,'database':lambda:c,'audit':lambda *a:None,'HTTPException':HTTPException,'RedirectResponse':lambda *a:a,'public_path':lambda s:s}
  n=funcs['user_payment_price_update'];n.decorator_list=[];exec(compile(ast.Module(body=[n],type_ignores=[]),str(path),'exec'),ns)
  try:ns[n.name](types.SimpleNamespace(session={}),1,'0');out[key]=c.updated
  except HTTPException:out[key]=False
 out['zero_display_preserved']='price_value = "" if row.get("custom_payment_price") is None' in s
 for setting,default in [('MAKE_LATEST',True),('DRAFT',False)]:
  n=next(n for n in ast.walk(funcs['_save_settings']) if isinstance(n,ast.Dict) and any(isinstance(k,ast.Constant) and k.value=='GITHUB_RELEASE_'+setting for k in n.keys))
  expr=next(v for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant) and k.value=='GITHUB_RELEASE_'+setting)
  out['preserve_hidden_'+setting]=eval(compile(ast.Expression(expr),'<field>','eval'),{'checked':lambda x:False,'github_configurable':False,'config':types.SimpleNamespace(**{'GITHUB_RELEASE_'+setting:True})}) is True
 if '_panel_user_link' in funcs:
  n=funcs['_panel_user_link'];ns={'Any':object,'html':html};exec(compile(ast.Module(body=[n],type_ignores=[]),'<link>','exec'),ns)
  out['internal_id_link']='/users/id/123' in ns[n.name](123,'<test>') and '&lt;test&gt;' in ns[n.name](123,'<test>') and 'href' not in ns[n.name](0,'test') and '/users/id/-123' in ns[n.name](-123,'local') and 'href' not in ns[n.name](9007199254740993,'unsafe')
 else:out['internal_id_link']=False
 return out

def test_web_identity_settings_and_legacy_zero_regressions():
    actual = probe_web(Path(__file__).resolve().parents[1] / "app/webapp.py")
    assert actual == {"existing_zero": True, "new_zero": False,
                      "nonzero_to_zero": False, "zero_display_preserved": True,
                      "preserve_hidden_MAKE_LATEST": True, "preserve_hidden_DRAFT": True,
                      "internal_id_link": True}
