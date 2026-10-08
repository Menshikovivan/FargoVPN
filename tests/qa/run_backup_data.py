"""Run read-model checks from fargovpn.sql/xui.db in a PRIVATE directory.
Never executes backup configuration, SQL functions, systemd or network calls.
Creates data.sqlite and sensitive HTML there; do not distribute this directory.
This is a SQLite COPY read model, not a PostgreSQL restoration test.
"""
import sys,importlib.util,logging,json,sqlite3,time,os
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[2];private=Path(sys.argv[1]).resolve();private.mkdir(exist_ok=True);os.chmod(private,0o700)
sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests/qa'))
from backup_fixture import build_fixture
fixture=private/'data.sqlite';fixture.unlink(missing_ok=True)
counts=build_fixture(private/'fargovpn.sql',fixture);os.chmod(fixture,0o600)
spec=importlib.util.spec_from_file_location('config',root/'config.example.py');config=importlib.util.module_from_spec(spec);sys.modules['config']=config;spec.loader.exec_module(config)
config.BOT_TOKEN='';config.ADMIN_IDS=[];config.MASTER_API_TOKEN='offline';config.DB_PATH=str(fixture);config.WEB_PUBLIC_PREFIX='/qa-panel';config.WEB_USERNAME='menshikovivan';config.UPDATE_DIR=str(private/'updates');config.XUI_MIN_REQUEST_INTERVAL_MS=0
import psutil
psutil.net_io_counters=lambda:type('Counters',(),{'bytes_recv':0,'bytes_sent':0})()
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
import services.xui_api as xui
original=sqlite3.connect
def connect(*a,**kw):
 c=original(fixture,timeout=20);c.row_factory=sqlite3.Row;return c
w.database_adapter.connect=connect
w.user_events._SCHEMA_READY.add(str(fixture));w.push_service._migrated_paths.add(str(fixture))
xdb=original('file:'+str(private/'xui.db')+'?mode=ro',uri=True);xdb.row_factory=sqlite3.Row
inbounds=[]
for row in xdb.execute('SELECT * FROM inbounds'):
 item=dict(row);item['clientStats']=[dict(s) for s in xdb.execute('SELECT * FROM client_traffics WHERE inbound_id=?',(item['id'],))];inbounds.append(item)
xdb.close()
import httpx
def offline(method,path,**kw):
 if path.endswith('inbounds/list'):return {'success':True,'obj':inbounds}
 if path.endswith('clients/list'):
  req=httpx.Request('GET','https://offline.invalid');raise httpx.HTTPStatusError('offline legacy read',request=req,response=httpx.Response(404,request=req))
 raise RuntimeError('Live telemetry/online disabled: offline backup')
xui.request_json_sync=offline
snapshot=xui.fetch_snapshot_sync(force=True)
assert snapshot.get('clients') and not snapshot.get('stale')
w.fetch_and_sync=lambda **kw:snapshot
users,_=w.live_users()
local_positive={int(row[0]) for row in connect().execute('SELECT tg_id FROM users WHERE tg_id>0')}
assert local_positive.issubset({u['tg_id'] for u in users}), 'Local Telegram users disappeared after merge'
assert all(isinstance(u['tg_id'],int) and u['traffic_used']>=0 for u in users)
# Offline source must never contact Telegram/GitHub/3x-ui transports.
w.httpx.get=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('network disabled'))
w.httpx.post=w.httpx.get
from starlette.requests import Request
out=private/'rendered';out.mkdir(exist_ok=True);os.chmod(out,0o700)
results={}
for name,fn in [('users',w.users_page),('messages',w.messages_page),('payments',w.payments),('audit',w.audit_page),('reminders',w.reminders_page),('settings',w.settings)]:
 req=Request({'type':'http','method':'GET','path':'/'+name,'query_string':b'tab=notifications','headers':[],'session':{'auth':True,'user':'menshikovivan','csrf_token':'offline-csrf'},'scheme':'https','server':('offline.invalid',443)})
 if name=='settings':w.inbound_selection_status_sync=lambda **kw:{'ok':False,'items':[],'selected':[],'missing':[],'error':'Offline backup: live API disabled'}
 w.service_state=lambda *a:'unknown'
 html=fn(req);(out/(name+'.html')).write_text(html);os.chmod(out/(name+'.html'),0o600);results[name]='render passed'
req=Request({'type':'http','method':'GET','path':'/api/panel/push/logs','headers':[],'session':{'auth':True,'user':'menshikovivan'},'scheme':'https','server':('offline.invalid',443)})
logs=w.panel_push_logs(req);assert logs['ok'] and isinstance(logs['logs'],list)
results['push_logs']='passed; rows='+str(len(logs['logs']))
assert w.user_events.count_events()>0
result={'version':w.update_manager.current_version(),'tables':counts,'xui_inbounds':len(inbounds),'xui_clients_after_dedup':len(snapshot['clients']),'merged_users':len(users),'pages':results,'network':'disabled','database':'SQLite COPY reader; not real PostgreSQL restore'}
(private/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
