#!/usr/bin/env python3
"""Standalone PostgreSQL QA seeder; ignores production config and refuses non-QA DSNs."""
import argparse,importlib.util,os,pathlib,sys
from urllib.parse import urlsplit
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
def main():
 p=argparse.ArgumentParser();p.add_argument('--count',type=int,default=1000);p.add_argument('--clean',action='store_true');a=p.parse_args()
 dsn=os.environ.get('QA_DATABASE_URL','');u=urlsplit(dsn.replace('postgresql+psycopg','postgresql'))
 if u.hostname not in ('localhost','127.0.0.1') or not u.path.endswith('_qa'):raise SystemExit('Refuse non-test DSN; QA_DATABASE_URL must use loopback and database ending _qa')
 if not 200<=a.count<=10000:raise SystemExit('--count must be 200..10000')
 os.environ['FARGOVPN_DATABASE_URL']=dsn
 spec=importlib.util.spec_from_file_location('config',ROOT/'config.example.py');c=importlib.util.module_from_spec(spec);sys.modules['config']=c;spec.loader.exec_module(c);c.DATABASE_URL=dsn
 import db,init_db
 init_db.migrate()
 from seed_regression import NAMES
 with db.connect('qa') as conn:
  if a.clean:conn.execute('DELETE FROM users WHERE tg_id>=900000 AND tg_id<910000 AND identity_source=?',('qa-seeder',))
  else:
   for i in range(a.count):
    conn.execute('INSERT INTO users(tg_id,username,display_name,enable,expiry_time,registered_at,identity_source) VALUES(?,?,?,?,?,?,?) ON CONFLICT(tg_id) DO NOTHING',(900000+i,NAMES[i%8]+' '+str(i),NAMES[i%8],int(i%3!=2),1800000000000 if i%3==0 else 1700000000000,'2026-01-01','qa-seeder'))
  conn.commit();print('QA rows:',conn.execute('SELECT count(*) FROM users WHERE identity_source=?',('qa-seeder',)).fetchone()[0])
if __name__=='__main__':main()
