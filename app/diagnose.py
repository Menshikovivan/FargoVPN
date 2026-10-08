#!/usr/bin/env python3
"""Independent read-only FargoVPN evidence collector. Never import app/config."""
import argparse, ast, base64, datetime, hashlib, importlib.metadata, json, os, pathlib, re, shutil, subprocess, sys, urllib.request, urllib.error
SECRET = re.compile(r'token|password|secret|private|cookie|dsn|database_url|api_key', re.I)
def read_config(root):
    values = {}
    p = root / 'config.py'
    if p.exists():
        for n in ast.parse(p.read_text(encoding='utf-8')).body:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        try: values[t.id] = ast.literal_eval(n.value)
                        except (ValueError, TypeError): pass
    return values

def redact(text, values=()):
    text = str(text)
    for value in sorted((str(x) for x in values if x and len(str(x)) >= 4), key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', text, flags=re.S)
    text = re.sub(r'''(?i)(token|password|secret|cookie|authorization|api[_-]?key|auth|p256dh)([\s\"\x27:=]+)([\"\x27])[^\n]*?\3''', r'\1=[REDACTED]', text)
    text = re.sub(r'(?i)(https?://api\.telegram\.org/bot)[^/\s]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(authorization|set-cookie|cookie|password|token|secret|api[_-]?key|auth|p256dh)([\s\"\x27:=]+)[^\s,;]+', r'\1\2[REDACTED]', text)
    text = re.sub(r'(?i)(postgres(?:ql)?(?:\+psycopg)?://)[^\s]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?im)(authorization|set-cookie|cookie)([^\n]*)', r'\1 [REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[JWT REDACTED]', text)
    text = re.sub(r'https?://[^\s\"<>]+', '[URL REDACTED]', text)
    return text

def collect(root, base_url='', output_dir=None):
    root = pathlib.Path(root).resolve(); results = []; lines = []
    try: cfg = read_config(root)
    except Exception: cfg = {}; results.append(('config syntax','FAIL'))
    secrets = [v for k,v in cfg.items() if SECRET.search(k)]
    secrets += [v for k,v in os.environ.items() if SECRET.search(k)]
    def record(name, status, detail=''):
        results.append((name,status)); lines.append(f'[{status}] {name}\n{redact(detail,secrets)}')
    def command(name, argv):
        if not shutil.which(argv[0]): record(name,'SKIP','Command unavailable'); return
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=15)
            record(name,'PASS' if r.returncode==0 else 'FAIL',(r.stdout+r.stderr)[-50000:])
        except Exception as e: record(name,'FAIL',type(e).__name__)
    record('version','PASS' if (root/'VERSION').is_file() else 'FAIL',(root/'VERSION').read_text().strip() if (root/'VERSION').is_file() else 'missing')
    record('python','PASS',sys.version)
    record('packages','PASS','\n'.join(sorted(f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions())))
    for unit in ['vpn-service-bot','vpn-service-web','x-ui','nginx']:
        command('service '+unit,['systemctl','is-active',unit])
        command('journal '+unit,['journalctl','-u',unit,'-n','150','--no-pager','-o','cat'])
    for name in ['/var/log/vpn_bot.log','/var/log/vpn-service-web.log','/var/log/nginx/error.log']:
        p=pathlib.Path(name)
        try:
            with p.open('rb') as h: h.seek(max(0,p.stat().st_size-65536)); data=h.read().decode('utf8','replace')
            record('log '+name,'PASS',data)
        except OSError: record('log '+name,'SKIP','unavailable')
    for name in ['webapp.py','static/panel.js','static/panel.css','service-worker.js','config.py']:
        p=root/name
        try: record('file '+name,'FAIL' if name=='config.py' and os.name=='posix' and p.stat().st_mode & 0o077 else 'PASS',f'size={p.stat().st_size} mode={oct(p.stat().st_mode & 0o777)} sha256={hashlib.sha256(p.read_bytes()).hexdigest()}')
        except OSError: record('file '+name,'FAIL','missing')
    migrations=root/'migrations'; record('migration files','PASS','\n'.join(p.name+' '+hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(migrations.glob('*.sql'))))
    disk=shutil.disk_usage(root);record('disk','PASS' if disk.free>100*1024*1024 else 'FAIL',f'free={disk.free} total={disk.total}')
    command('nginx effective configuration',['nginx','-T'])
    command('resources',['free','-m']);command('uptime',['uptime'])
    keypath=pathlib.Path(str(cfg.get('PUSH_VAPID_PRIVATE_KEY_FILE',cfg.get('PUSH_VAPID_PRIVATE_KEY_PATH','/var/lib/vpn-service/vapid_private.pem'))))
    if not keypath.is_absolute(): keypath=root/keypath
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key=serialization.load_pem_private_key(keypath.read_bytes(),password=None)
        if not isinstance(key,ec.EllipticCurvePrivateKey) or key.curve.name!='secp256r1': raise ValueError('not P-256')
        public=base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)).decode().rstrip('=')
        ok=public==str(cfg.get('PUSH_VAPID_PUBLIC_KEY','')).strip()
        record('VAPID pair','PASS' if ok else 'FAIL','matching P-256 pair' if ok else 'public key missing/mismatch; no repair performed')
    except Exception as e: record('VAPID pair','FAIL',type(e).__name__+'; key unreadable/invalid or cryptography unavailable')
    dsn=os.getenv('FARGOVPN_DATABASE_URL') or os.getenv('DATABASE_URL') or cfg.get('DATABASE_URL','')
    try:
        import psycopg
        dsn=str(dsn).replace('postgresql+psycopg://','postgresql://')
        with psycopg.connect(dsn,connect_timeout=5,options='-c default_transaction_read_only=on -c statement_timeout=5000') as conn:
            with conn.cursor() as c:
                c.execute('SHOW transaction_read_only'); assert c.fetchone()[0]=='on'
                c.execute("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position")
                schema=c.fetchall(); record('database schema','PASS',json.dumps(schema))
                tables={r[0] for r in schema}
                for table in ['users','panel_push_subscriptions','push_subscriptions']:
                    if table in tables:
                        c.execute('SELECT count(*) FROM '+table);record('count '+table,'PASS',str(c.fetchone()[0]))
                    else: record('count '+table,'SKIP','table absent')
                if 'users' in tables:
                    c.execute("SELECT count(*) FROM users WHERE lower(coalesce(username,'')) LIKE %s",('%a%',));record('database search','PASS','matches='+str(c.fetchone()[0]))
                if 'panel_push_subscriptions' in tables:
                    c.execute('SELECT enabled,count(*) FROM panel_push_subscriptions GROUP BY enabled');record('push enabled counts','PASS',str(c.fetchall()))
                    c.execute('SELECT endpoint,p256dh,auth FROM panel_push_subscriptions')
                    invalid=0;total=0
                    for endpoint,point,auth in c.fetchall():
                        total+=1
                        try:
                            decode=lambda v:base64.urlsafe_b64decode(str(v)+'='*((4-len(str(v))%4)%4))
                            good=str(endpoint).startswith('https://') and len(decode(auth))==16
                            from cryptography.hazmat.primitives.asymmetric import ec
                            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),decode(point))
                            if not good:invalid+=1
                        except Exception:invalid+=1
                    record('push subscription format','PASS' if invalid==0 else 'FAIL',f'total={total} invalid={invalid}')
    except Exception as e: record('database read-only','FAIL',type(e).__name__+'; connection/configuration unavailable')
    # GitHub is diagnostic-only here: GET requests only, never POST/PATCH/PUT/DELETE.
    owner = str(cfg.get('GITHUB_REPOSITORY_OWNER','') or '').strip()
    repo = str(cfg.get('GITHUB_REPOSITORY_NAME','FargoVPN') or 'FargoVPN').strip()
    token = str(cfg.get('GITHUB_API_TOKEN','') or '').strip()
    api_base = str(cfg.get('GITHUB_API_BASE_URL','https://api.github.com') or 'https://api.github.com').rstrip('/')
    if owner and repo and token:
        try:
            headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+token,'X-GitHub-Api-Version':'2022-11-28'}
            req=urllib.request.Request(api_base+'/user',headers=headers,method='GET')
            with urllib.request.urlopen(req,timeout=10) as resp: user=json.loads(resp.read(512*1024))
            req=urllib.request.Request(api_base+'/repos/'+urllib.parse.quote(owner,safe='')+'/'+urllib.parse.quote(repo,safe=''),headers=headers,method='GET')
            with urllib.request.urlopen(req,timeout=10) as resp: repository=json.loads(resp.read(512*1024))
            record('GitHub read-only access','PASS',f"login={user.get('login','—')} repo={repository.get('full_name','—')} private={repository.get('private','—')}; GET only")
        except urllib.error.HTTPError as e:
            record('GitHub read-only access','FAIL',f'HTTP {e.code}; GET only')
        except Exception as e:
            record('GitHub read-only access','FAIL',type(e).__name__+'; GET only')
    else:
        record('GitHub read-only access','SKIP','owner/repository/token are not configured; no write request attempted')

    if base_url:
        for path in ['/health','/login','/service-worker.js','/manifest.webmanifest','/static/panel.js','/static/panel.css']:
            try:
                with urllib.request.urlopen(base_url.rstrip('/')+path,timeout=10) as r:
                    data=r.read(4*1024*1024);ctype=r.headers.get('Content-Type',''); code=r.status
                    policies=r.headers.get_all('Content-Security-Policy') or []
                    if policies:
                        record('CSP '+path,'FAIL' if any("script-src 'self';" in policy or "style-src 'self';" in policy for policy in policies) else 'PASS','\n'.join(policies))
                    ok=code==200 and bool(data)
                    if path.endswith('.js'):ok=ok and 'javascript' in ctype
                    record('HTTP '+path,'PASS' if ok else 'FAIL',f'code={code} type={ctype} sha256={hashlib.sha256(data).hexdigest()} size={len(data)}')
            except urllib.error.HTTPError as e: record('HTTP '+path,'FAIL','HTTP '+str(e.code))
            except Exception as e: record('HTTP '+path,'FAIL',type(e).__name__)
    else: record('HTTP','SKIP','Supply --url including public prefix')
    record('authenticated search/filter API','SKIP','Old users GET synchronizes 3x-ui; cannot safely invoke under strictly read-only policy. Use isolated browser regression tests.')
    record('push endpoint actions','SKIP','config/status/log GET may generate keys, migrate tables or write logs. No subscribe/test/unsubscribe on production in read-only collector.')
    cookie=os.getenv('FARGOVPN_DIAG_COOKIE','')
    if base_url and cookie:
        class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                from urllib.parse import urlsplit
                if urlsplit(newurl).netloc!=urlsplit(base_url).netloc:raise RuntimeError('Cross-origin redirect refused')
                return super().redirect_request(req,fp,code,msg,headers,newurl)
        opener=urllib.request.build_opener(SameOriginRedirect())
        try:
            def read_api(query):
                req=urllib.request.Request(base_url.rstrip('/')+'/api/diagnostics/read-only'+query,headers={'Cookie':cookie,'Accept':'application/json'})
                with opener.open(req,timeout=10) as response:data=json.loads(response.read(1024*1024))
                if data.get('read_only') is not True or not isinstance(data.get('items'),list) or not isinstance(data.get('count'),int):raise ValueError('API format')
                return data
            from urllib.parse import urlencode
            data=read_api('');record('API format/count','PASS',f'count={data["count"]} items={len(data["items"])}')
            if data['items']:
                item=data['items'][0];name=item.get('username') or item.get('display_name') or ''
                found=read_api('?'+urlencode({'q':name}));record('API search','PASS' if found['count']>0 else 'FAIL','matched='+str(found['count']))
            else:record('API search','SKIP','No users')
            for status in ['active','expired','blocked']:
                filtered=read_api('?status='+status);record('API filter '+status,'PASS' if filtered['count']<=data['count'] else 'FAIL','count='+str(filtered['count']))
            record('API filter online','SKIP','Online state requires live 3x-ui and cannot be inferred safely from stored timestamps')
            record('API push subscription state','PASS' if isinstance(data.get('push',{}).get('enabled'),int) else 'FAIL','Count-only response; no delivery side effects')
        except urllib.error.HTTPError as e:record('read-only diagnostic API','SKIP' if e.code in (401,404) else 'FAIL','HTTP '+str(e.code)+'; old releases do not provide this API')
        except Exception as e:record('read-only diagnostic API','FAIL',type(e).__name__)
    else:record('read-only diagnostic API','SKIP','Provide --url and FARGOVPN_DIAG_COOKIE or --cookie-file for authenticated checks')
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f'); name='fargovpn_diag_'+stamp+'.log'
    candidates=[pathlib.Path(output_dir)] if output_dir else [pathlib.Path('/var/log'),pathlib.Path(os.getenv('TMPDIR',os.getenv('TEMP','/tmp')))]
    for folder in candidates:
        try:
            folder.mkdir(parents=True,exist_ok=True);p=folder/name
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf8') as h:h.write('\n\n'.join(lines)+'\n\nSUMMARY\n'+'\n'.join(f'{s} {n}' for n,s in results))
            return {'path':str(p.resolve()),'checks':[{'name':n,'status':s} for n,s in results]}
        except OSError: continue
    raise RuntimeError('No writable report directory')


def effective_nginx_contract(stream=None) -> None:
    import sys as _sys
    out = stream or _sys.stdout
    print("\n[READ-ONLY] EFFECTIVE NGINX CSP CONTRACT", file=out)
    try:
        proc=subprocess.run(["nginx","-T"],capture_output=True,text=True,timeout=15)
        dump=(proc.stdout or "")+(proc.stderr or "")
        has_hide="proxy_hide_header Content-Security-Policy" in dump
        has_csp=bool(re.search(r"add_header\s+Content-Security-Policy[^;]*unsafe-inline",dump))
        print(f"managed_csp_hide={has_hide} csp_unsafe_inline={has_csp}", file=out)
        print("[PASS] nginx FargoVPN CSP contract" if has_hide and has_csp else "[FAIL] nginx FargoVPN CSP contract", file=out)
    except Exception as exc:
        print(f"[SKIP] nginx contract: {redact(str(exc))}", file=out)

def main():
    p=argparse.ArgumentParser();p.add_argument('--app-dir',default=str(pathlib.Path(__file__).resolve().parent));p.add_argument('--url',default='');p.add_argument('--output-dir');p.add_argument('--json',action='store_true');p.add_argument('--cookie-file',type=pathlib.Path);a=p.parse_args()
    # JSON mode is consumed by diagnostic_jobs.py; stdout must contain exactly one JSON document.
    effective_nginx_contract(stream=sys.stderr if a.json else sys.stdout)
    if a.cookie_file:os.environ['FARGOVPN_DIAG_COOKIE']=a.cookie_file.read_text().strip()
    r=collect(a.app_dir,a.url,a.output_dir)
    if a.json: print(json.dumps(r, ensure_ascii=False))
    else:
        for c in r['checks']: print(c['status'],c['name'])
        print('LOG_FILE='+r['path'])
if __name__=='__main__':main()
