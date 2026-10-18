"""Manual acceptance against official 3x-ui; excluded from automatic pytest collection.
Requires a FRESH isolated panel, synthetic audit credentials, loopback18081 /audit/.
Creates a loopback18443 inbound and synthetic clients; never target a real installation.
"""
import sys, json, uuid, time, sqlite3, traceback
import argparse
parser=argparse.ArgumentParser(description='Explicit destructive acceptance test for a FRESH disposable 3x-ui only. Never use a server database.')
parser.add_argument('--allow-disposable-loopback',action='store_true',required=True)
parser.add_argument('--app-dir',default='/root/FargoVPN/app')
parser.add_argument('--db-path',default='/root/xui-api-audit/db/x-ui.db')
args=parser.parse_args()
from pathlib import Path
assert Path(args.db_path).is_file(), 'Create the disposable 3x-ui first'
with sqlite3.connect(Path(args.db_path).resolve().as_uri()+'?mode=ro',uri=True) as db:
 settings=dict(db.execute('SELECT key,value FROM settings'))
 assert str(settings.get('webPort'))=='18081' and settings.get('webListen')=='127.0.0.1', 'Only disposable loopback:18081 is supported'
 assert settings.get('webBasePath')=='/audit/', 'Unexpected disposable panel base path'
 assert db.execute('SELECT count(*) FROM inbounds').fetchone()[0]==0, 'Refusing a nonempty panel; use a fresh isolated database'
sys.path.insert(0,args.app_dir)
import types
config=types.ModuleType("config")
sys.modules["config"]=config
config.XUI_DB_PATH=args.db_path
config.XUI_DB_ENV_FILE='/root/xui-api-audit/no-env'
config.XUI_POSTGRES_DSN=''
config.XUI_INTERNAL_BASE_URL=''
config.XUI_INTERNAL_AUTO_DETECT=True
config.BASE_URL='http://127.0.0.1:18081/audit'
config.MASTER_API_TOKEN=''
config.XUI_USERNAME='audit'
config.XUI_PASSWORD='isolated-audit-12345'
from services import xui_api as x

def check(label, value):
    assert value, label
    print('PASS', label, flush=True)
check('SQLite discovery port/base path',x._base_url()=='http://127.0.0.1:18081/audit')
check('cookie auth + list inbounds', x.request_json_sync('GET','panel/api/inbounds/list')['success'])
payload={'up':0,'down':0,'total':0,'remark':'isolated-audit-loopback','enable':True,'expiryTime':0,'listen':'127.0.0.1','port':18443,'protocol':'vless','settings':json.dumps({'clients':[],'decryption':'none','fallbacks':[]}), 'streamSettings':json.dumps({'network':'tcp','security':'none','tcpSettings':{'acceptProxyProtocol':False,'header':{'type':'none'}}}), 'sniffing':json.dumps({'enabled':False,'destOverride':['http','tls']}),'allocate':json.dumps({'strategy':'always','refresh':5,'concurrency':3})}
r=x.request_json_sync('POST','panel/api/inbounds/add',payload)
print('inbound creation',json.dumps(r),flush=True)
inbound=r['obj']['id']
def client(email):
 return {'id':str(uuid.uuid4()),'email':email,'enable':True,'expiryTime':0,'limitIp':0,'totalGB':0,'subId':uuid.uuid4().hex[:16],'tgId':0,'reset':0,'flow':''}
sentinel=client('isolated-preserve@example.test'); changed=client('isolated-change@example.test')
x.add_client_sync(sentinel,[inbound]); x.add_client_sync(changed,[inbound])
check('create clients + inbound confirmation',set(x.get_client_record_sync(changed['email'])['inbound_ids'])=={inbound})
snapshot=x.get_client_record_sync(sentinel['email'])['client']
x.update_client_sync(changed['email'],{'comment':'updated-by-fargovpn-audit','totalGB':1048576})
record=x.get_client_record_sync(changed['email'])
check('update client roundtrip',record['client']['comment']=='updated-by-fargovpn-audit' and record['client']['totalGB']==1048576)
check('unrelated client unchanged after update',x.get_client_record_sync(sentinel['email'])['client']==snapshot)
x.delete_client_sync(changed['email'])
try:
 x.get_client_record_sync(changed['email'])
except Exception:
 print('PASS deleted client absent',flush=True)
else:
 raise AssertionError('deleted client still present')
check('unrelated client unchanged after delete',x.get_client_record_sync(sentinel['email'])['client']==snapshot)
print('SMOKE_UUID='+sentinel['id'],flush=True)
print('ALL_API_CHECKS_PASSED',flush=True)
