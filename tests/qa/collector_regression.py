import importlib.util,json,os,pathlib,tempfile
root=pathlib.Path(__file__).resolve().parents[2];spec=importlib.util.spec_from_file_location('diag',root/'diagnose.py');d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
# Verify API collector branch without network or production writes.
class Reply:
 status=200
 headers={'Content-Type':'application/javascript'}
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self,*a):return b'{}'
class Opener:
 def open(self,req,**kw):
  from urllib.parse import parse_qs,urlsplit
  q=parse_qs(urlsplit(req.full_url).query);r=Reply()
  count=1 if q.get('status',['all'])[0]=='all' else 0
  payload={'read_only':True,'count':count,'items':[{'username':'qa sample'}] if count else [],'push':{'enabled':0}}
  r.read=lambda *a:json.dumps(payload).encode();return r
orig=d.urllib.request.urlopen;d.urllib.request.urlopen=lambda *a,**kw:Reply();d.urllib.request.build_opener=lambda *a:Opener()
os.environ['FARGOVPN_DIAG_COOKIE']='session=synthetic-qa-secret'
folder=pathlib.Path(tempfile.mkdtemp()).resolve();fixture=folder/'source';fixture.mkdir();(fixture/'VERSION').write_text('5.0.5');(fixture/'config.py').write_text('DATABASE_URL=""\n');os.environ.pop('FARGOVPN_DATABASE_URL',None);os.environ.pop('DATABASE_URL',None);r=d.collect(fixture,'https://panel.example.test/qa-panel',folder)
api=[x for x in r['checks'] if x['name'].startswith('API')];assert all(x['status'] in ['PASS','SKIP'] for x in api),api
text=pathlib.Path(r['path']).read_text(encoding='utf8');assert 'synthetic-qa-secret' not in text
print('collector API shape/search/filter/count branch and cookie masking PASS')

secret='secret with spaces'
assert secret not in d.redact('password="'+secret+'"')
(fixture/'config.py').write_text('raise RuntimeError("must not execute")\nBOT_TOKEN="synthetic-secret"\n')
assert d.read_config(fixture)['BOT_TOKEN']=='synthetic-secret'
print('collector non-executable config / quoted-secret masking PASS')
