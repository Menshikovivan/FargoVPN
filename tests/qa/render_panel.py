import sys,importlib.util,logging,json,re
from pathlib import Path
from unittest.mock import patch
root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root/'app'));sys.path.insert(0,str(root));sys.path.insert(0,str(root.parent/'testdeps'))
spec=importlib.util.spec_from_file_location('config',root/'app'/'config.example.py');config=importlib.util.module_from_spec(spec);sys.modules['config']=config;spec.loader.exec_module(config)
config.BOT_TOKEN='';config.MASTER_API_TOKEN='test-only';config.WEB_PUBLIC_PREFIX='/qa-panel';config.DB_PATH='unused';config.WEB_USERNAME='admin'
import psutil
from types import SimpleNamespace as NS
psutil.net_io_counters=lambda:NS(bytes_recv=0,bytes_sent=0)
with patch('logging.FileHandler',lambda *a,**kw:logging.NullHandler()):import webapp as w
w.inbound_selection_status_sync=lambda **kw:{'ok':True,'items':[],'selected':[],'missing':[],'error':''}
w.service_state=lambda *a:'active';w.update_manager.cached_update_info=lambda:{};w.user_events.unread_messages_summary=lambda **kw:{'total':0,'items':[]}
w.update_manager.read_status=lambda:{'state':'idle'};w.update_manager.list_preupdate_backups=lambda:[];w.update_manager.latest_local_update=lambda:None
w.fetch_control_snapshot_sync=lambda:{'ts':1,'status':{'cpu':12,'xray_state':'running','xray_version':'v1'},'nodes':[],'fail2ban':{},'error':''}
psutil.process_iter=lambda *a:[];psutil.virtual_memory=lambda:NS(percent=25,used=1024,total=4096);psutil.swap_memory=lambda:NS(percent=0,used=0,total=1024);psutil.disk_usage=lambda *a:NS(percent=10,free=1000000);psutil.cpu_percent=lambda *a:12;psutil.boot_time=lambda:1700000000;psutil.getloadavg=lambda:(.1,.2,.3)
users=[dict(tg_id=i+1,username='user'+str(i),email='email'+str(i),comment='User '+str(i),enable=i%3!=2,active=i%3==0,online=i%5==0,remaining_days=i,traffic_used=i*10,quota_remaining=110-i,last_online_ts=i,registered_at=f'2026-01-{i%28+1:02d}',uuid='uuid',sub_id='sub',expiry_time=1800000000000,quota_total=0,traffic_up=0,traffic_down=0,up=0,down=0,total=0) for i in range(110)]
w.live_users=lambda:([dict(x) for x in users],{'clients':[],'stale':False,'error':''})
class Cursor:
 def execute(self,*args,**kwargs):return self
 def fetchall(self):return []
 def fetchone(self):return (0,)*12
 def __iter__(self):return iter([])
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def commit(self):pass
 def close(self):pass
 def rollback(self):pass
w.database=lambda:Cursor()
w.database_adapter.connect=lambda *a,**kw:Cursor()
w.user_events.conversation_users=lambda **kw:[]
w.user_events.count_events=lambda **kw:0
w.user_events.list_events=lambda **kw:[]
w.user_events.all_events=lambda **kw:[]
w.user_events.mark_conversation_opened=lambda *a,**kw: None
w.user_events.mark_messages_read=lambda *a,**kw: None
w.get_user=lambda tg_id: {**users[0], 'tg_id': int(tg_id), 'referred_by_tg_id': 0, 'identity_source': 'qa', 'notes': ''}
w.referral_rewards.referral_stats=lambda *a,**kw: {'invited':0,'paid':0,'rewards':0,'days':0}
w.fetch_client_extra_sync=lambda *a,**kw: {'traffic':{},'ips':[],'error':''}
w.get_client_record_sync=lambda *a,**kw: {'client':{}}
w.registration_access.authorize_admin_user_sync=lambda *a,**kw: None
w._read_service_logs=lambda *a:'test log'
w.read_live_state=lambda:{}
w.restore_manager.read_restore_state=lambda:{}
w.broadcast_manager.read_status=lambda:{}
w.fetch_and_sync=lambda **kw:{'clients':[],'stale':False}
w.service_audit.audit=lambda:{'healthy':True}
w.platform_diagnostics.database_report=lambda *a,**kw:{'healthy':True}
w.platform_diagnostics.storage_report=lambda *a,**kw:{'healthy':True}
w.platform_diagnostics.config_permissions_report=lambda:{'healthy':True}
config.BACKUP_DIR=str(Path(sys.argv[2]).parent/'backup')
from starlette.requests import Request
out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
for name,fn,kw in [('login',w.login_page,{}),('dashboard',w.dashboard,{}),('settings',w.settings,{}),('users',w.users_page,{'q':'User 1','status':'active'}),('user-detail',w.user_detail_page,{'tg_id':1}),('new-user',w.new_user_page,{}),('monitoring',w.monitoring,{}),('updates',w.updates_page,{}),('messages',w.messages_page,{}),('broadcast',w.broadcast_page,{}),('payments',w.payments,{}),('backups',w.backups,{}),('logs',w.logs,{}),('audit',w.audit_page,{}),('reminders',w.reminders_page,{}),('diagnostics',w.diagnostics,{}),('subscription-tools',w.subscription_tools_page,{})]:
 req=Request({'type':'http','method':'GET','path':'/'+name,'query_string':b'tab=notifications' if name=='settings' else b'','headers':[],'session':{'auth':True,'user':'admin','csrf_token':'csrf'},'scheme':'https','server':('panel.example.test',443)})
 try:text=fn(req,**kw)
 except Exception as e:raise RuntimeError(f'{name}: {e}') from e
 (out/(name+'.html')).write_text(text)
 print(name,len(text))
