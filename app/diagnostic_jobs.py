"""Bounded read-only diagnostic jobs, shared across web workers via private files."""
import concurrent.futures, json, os, pathlib, re, subprocess, sys, tempfile, time, uuid
POOL=concurrent.futures.ThreadPoolExecutor(max_workers=1)
DIRECTORY=pathlib.Path(tempfile.gettempdir())/'fargovpn-diagnostics'

def folder():
    DIRECTORY.mkdir(mode=0o700,exist_ok=True)
    if DIRECTORY.is_symlink() or (hasattr(os,'getuid') and DIRECTORY.stat().st_uid != os.getuid()):
        raise RuntimeError('Unsafe diagnostic directory')
    os.chmod(DIRECTORY,0o700)
    return DIRECTORY

def save(job,data):
    p=folder()/(job+'.json');temp=p.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf8') as h:json.dump(data,h)
    temp.replace(p)

def read(job,owner):
    if not re.fullmatch('[a-f0-9]{32}',job):raise KeyError('Unknown job')
    data=json.loads((folder()/(job+'.json')).read_text(encoding='utf8'))
    if data['owner']!=owner:raise KeyError('Unknown job')
    if data['state']=='running' and time.time()-data['started']>240:
        data.update(state='failed',error='Диагностика прервана или превышен таймаут')
    return data

def start(root,owner,url="",cookie=""):
    # File lock prevents parallel collectors across gunicorn workers.
    import fcntl
    gate=open(folder()/'collector.lock','a+');os.chmod(gate.name,0o600)
    try:fcntl.flock(gate.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:gate.close();raise RuntimeError('Диагностика уже выполняется')
    job=uuid.uuid4().hex;state={'owner':owner,'state':'running','started':time.time(),'error':''};save(job,state)
    def run():
        try:
            result=subprocess.run([sys.executable,str(pathlib.Path(root)/'diagnose.py'),'--app-dir',str(root),'--output-dir',str(folder()),'--json',*(['--url',url] if url else [])],env={**os.environ,'FARGOVPN_DIAG_COOKIE':cookie},capture_output=True,text=True,encoding='utf8',timeout=210)
            if result.returncode:raise RuntimeError('Сборщик завершился с кодом '+str(result.returncode))
            raw=result.stdout.strip()
            try:
                report=json.loads(raw)
            except json.JSONDecodeError as first_error:
                # Keep the worker tolerant of accidental diagnostic noise, but only accept a complete JSON object.
                start=raw.find('{')
                if start < 0: raise first_error
                try: report=json.JSONDecoder().raw_decode(raw[start:])[0]
                except json.JSONDecodeError: raise first_error
            path=pathlib.Path(report['path']).resolve()
            if path.parent!=folder().resolve() or not path.name.startswith('fargovpn_diag_'):raise RuntimeError('Invalid report path')
            state.update(state='completed',path=str(path),checks=report['checks'])
        except subprocess.TimeoutExpired:state.update(state='failed',error='Превышен таймаут 210 секунд')
        except Exception as e:state.update(state='failed',error=str(e))
        finally:save(job,state);gate.close()
    POOL.submit(run);return job
