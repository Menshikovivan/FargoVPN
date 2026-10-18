import sys,pathlib,json,time,os
from playwright.sync_api import sync_playwright
root=pathlib.Path(sys.argv[1]).resolve();render=pathlib.Path(sys.argv[2]).resolve();out=pathlib.Path(sys.argv[3]);strict=len(sys.argv)>4 and sys.argv[4]=='strict'
import threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')));self.do_GET()
 def do_GET(self):
  path=urlsplit(self.path).path.removeprefix('/qa-panel')
  requests.append((self.command,path))
  csp="default-src 'self';script-src 'self'"+('' if strict else " 'unsafe-inline'")+";style-src 'self' 'unsafe-inline';connect-src 'self';img-src 'self' data:"
  if path.startswith('/static/'):
   target=(root/path.lstrip('/')).resolve()
   if not target.is_relative_to(root/'static'):self.send_error(404);return
   data=target.read_bytes();ctype='application/javascript' if path.endswith('.js') else 'text/css'
  elif path=='/service-worker.js':
   data=(root/'service-worker.js').read_text(encoding='utf8').replace('__FARGOVPN_BASE__','/qa-panel/').replace('__FARGOVPN_VERSION__','5.0.5').replace('__FARGOVPN_CACHE__','fargovpn-static-vqa').encode();ctype='application/javascript'
  elif path in ['/users','/settings','/diagnostics']:data=(render/(path.lstrip('/')+'.html')).read_bytes();ctype='text/html'
  elif path=='/api/diagnostics/collect':data=b'{"job":"123","state":"running"}';ctype='application/json'
  elif path=='/api/diagnostics/collect/123':data=b'{"state":"completed","checks":[{"name":"isolated QA","status":"PASS"}]}';ctype='application/json'
  elif path.endswith('/download'):data=b'PASS isolated QA';ctype='text/plain'
  elif path.startswith('/api/'):
   from cryptography.hazmat.primitives.asymmetric import ec
   from cryptography.hazmat.primitives import serialization
   import base64
   key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
   data=json.dumps({'public_key':base64.urlsafe_b64encode(key).decode().rstrip('='),'ok':True,'logs':[],'lines':[],'count':0,'total':0,'queued':True,'subscriptions':0,'vapid_configured':True,'db_ok':True,'enabled':False}).encode();ctype='application/json'
  else:data=b'{}';ctype='application/json' 
  self.send_response(200);
  if path.endswith('/download'):self.send_header('Content-Disposition','attachment; filename=qa.log')
  self.send_header('Content-Type',ctype);self.send_header('Content-Security-Policy',csp);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();base='http://localhost:'+str(server.server_port)
results=[];console=[];errors=[];requests=[]
with sync_playwright() as p:
 b=p.chromium.launch(headless=True);page=b.new_page();page.set_default_timeout(2000);page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda m:console.append(m.text) if m.type=='error' else None)
 # All requests, including SW requests, use the real fixture HTTP server.
 def test(name,fn):
  started=time.perf_counter()
  try:fn();results.append({'test':name,'result':'PASS','ms':round((time.perf_counter()-started)*1000,1)})
  except Exception as e:results.append({'test':name,'result':'FAIL','detail':str(e)[:300]})
 page.goto(base+'/qa-panel/users');page.wait_for_timeout(150)
 def reset():
  page.locator('#user-filter-search').fill('');page.locator('[data-filter-status]').select_option('all');page.wait_for_timeout(160)
 def query(q,n):
  page.locator('#user-filter-search').fill(q);page.wait_for_timeout(180);assert int(page.locator('#users-visible-count').inner_text())==n;assert page.locator('.user-row:visible').count()==min(n,50)
 test('clear 1000 records',lambda:(reset(),query('',1000)))
 if '--probe' in sys.argv:
  test('search no result under strict CSP',lambda:query('NO_SUCH_QA',0))
  b.close();server.shutdown();out.write_text(json.dumps({'root':root.name,'tests':results,'page_errors':errors,'console_errors':console},ensure_ascii=False,indent=2),encoding='utf8');sys.exit(1 if any(t['result']=='FAIL' for t in results) else 0)

 for q,n in [('Alice Exact',125),('alice exact',125),('АЛЕКСЕЙ',125),('ёлкин',125),('user space',125),("O\'Brien <tag> & +",125),('用户',125),('emoji 😀',125),('NO_SUCH_QA',0),('Alice Exact 992',1)]:test('search '+q,lambda q=q,n=n:query(q,n))
 test('clear after empty',lambda:query('',1000))
 for st,n in [('active',334),('expired',333),('blocked',333),('online',200),('all',1000)]:
  def filter_case(st=st,n=n):
   page.locator('[data-filter-status]').select_option(st);page.wait_for_timeout(80);assert int(page.locator('#users-visible-count').inner_text())==n
  test('filter '+st,filter_case)
 def combo():
  reset();page.locator('[data-filter-status]').select_option('active');query('Alice Exact',42);page.locator('[data-filter-sort]').select_option('name');page.locator('[data-filter-order]').select_option('desc');assert 'Страница 1 из 1'==page.locator('[data-page-label]').inner_text();reset();page.locator('[data-page-next]').click();assert 'Страница 2 из 20'==page.locator('[data-page-label]').inner_text();assert page.locator('.user-row:visible').count()==50
 test('search/filter/sort/pagination combination',combo)
 for key in ['remaining','last_online','traffic','quota','name','registration']:
  
  if key=='registration' and root.name=='FargoVPN-4.9.3':results.append({'test':'sort registration','result':'SKIP','detail':'Not available in 4.9.3'});continue
  test('sort '+key,lambda key=key:page.locator('[data-filter-sort]').select_option(key))
 def stress():
  reset()
  for i in range(100):page.locator('#user-filter-search').fill('Alice Exact' if i%2 else 'NO_SUCH_QA')
  page.wait_for_timeout(200);assert int(page.locator('#users-visible-count').inner_text())==125
 test('rapid typing 100 changes / 1000 rows',stress)
 page.goto(base+'/qa-panel/settings?tab=notifications');page.wait_for_timeout(300)
 test('notifications tab opens',lambda:page.locator('[data-tab=notifications]').click())
 for id,path in [('panel-push-check','/api/panel/push/status'),('panel-push-test','/api/panel/push/test'),('panel-push-log-refresh','/api/panel/push/logs'),('app-log-refresh','/api/panel/app-log')]:
  def action(id=id,path=path):
   before=len(requests);page.locator('#'+id).click(timeout=3000);page.wait_for_timeout(180);assert any(u.startswith(path) for _,u in requests[before:]);assert not page.locator('#'+id).is_disabled()
  test('button '+id,action)
 def enable_failure():
  page.locator('#panel-push-enable').click(timeout=3000);page.wait_for_timeout(500)
  assert 'Разреш' in page.locator('#panel-push-help').inner_text() or 'Не удалось' in page.locator('#panel-push-help').inner_text()
  assert not page.locator('#panel-push-enable').is_disabled()
 test('enable permission-denied feedback',enable_failure)
 def disable():
  page.locator('#panel-push-disable').click(timeout=3000);page.wait_for_timeout(500)
  assert page.locator('#panel-push-status').inner_text()=='Выключены'
  assert not page.locator('#panel-push-disable').is_disabled()
 test('disable button / no browser subscription',disable)
 test('real Service Worker registration',lambda:page.evaluate('Promise.race([navigator.serviceWorker.ready.then(r=>{if(!r.active)throw Error("inactive worker");return true}),new Promise((_,reject)=>setTimeout(()=>reject(Error("worker timeout")),1500))])'))
 # New diagnostic UI only. Older releases must report absent feature accurately.
 if (root/'diagnose.py').is_file():
  page.goto(base+'/qa-panel/diagnostics');page.wait_for_timeout(100)
  def diag():
   page.locator('#diagnostic-collect').click();page.locator('#diagnostic-download').wait_for(state='visible');assert 'PASS' in page.locator('#diagnostic-progress').inner_text()
   with page.expect_download() as d:page.locator('#diagnostic-download').click()
   assert d.value.suggested_filename=='qa.log'
  test('diagnostic progress/download (HTTP fixtures)',diag)
 b.close()
server.shutdown()
out.write_text(json.dumps({'root':root.name,'strict_script_csp':strict,'tests':results,'page_errors':errors,'console_errors':console,'requests':requests},ensure_ascii=False,indent=2),encoding='utf8')
print(root.name,'strict='+str(strict),'PASS',sum(r['result']=='PASS' for r in results),'FAIL',sum(r['result']=='FAIL' for r in results),'pageerrors',len(errors),'console_errors',len(console))


if any(t["result"]=="FAIL" for t in results):sys.exit(1)
