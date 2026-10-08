"""Seed an isolated SQLite UI fixture; never connects to application PostgreSQL."""
import argparse,json,pathlib,sqlite3
NAMES=['Alice Exact','АЛЕКСЕЙ Ёлкин','Иван Петров',' user space ','O\'Brien <tag> & +','MixedCASE','用户','emoji 😀']
def seed(path,count=1000):
 p=pathlib.Path(path)
 if p.exists():raise RuntimeError('Refuse overwriting existing fixture')
 p.parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(p) as c:
  c.execute('CREATE TABLE qa_users(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
  for i in range(count):
   name=NAMES[i%len(NAMES)]+('' if i<len(NAMES) else ' '+str(i))
   item=dict(tg_id=i+1,username=name,email='email'+str(i),comment=name,enable=i%3!=2,active=i%3==0,online=i%5==0,remaining_days=i,traffic_used=i*10,quota_remaining=10000-i,last_online_ts=i,registered_at=f'2026-01-{i%28+1:02d}',uuid='uuid-'+str(i),sub_id='sub-'+str(i),expiry_time=1800000000000,quota_total=0,traffic_up=0,traffic_down=0,up=0,down=0,total=0)
   c.execute('INSERT INTO qa_users VALUES (?,?)',(i+1,json.dumps(item,ensure_ascii=False)))
 return p
def load(path):
 with sqlite3.connect(f'file:{pathlib.Path(path).resolve().as_posix()}?mode=ro',uri=True) as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM qa_users ORDER BY id')]
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('path');p.add_argument('--count',type=int,default=1000);p.add_argument('--clean',action='store_true');a=p.parse_args()
 if a.clean:
  target=pathlib.Path(a.path)
  with sqlite3.connect(f'file:{target.resolve().as_posix()}?mode=ro',uri=True) as c:
   tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   if tables!={'qa_users'}:raise RuntimeError('Refuse cleanup of non-QA SQLite database')
  target.unlink();print('Removed QA fixture')
 else:print(seed(a.path,a.count))
