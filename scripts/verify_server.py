#!/usr/bin/env python3
"""Read-only deployment checks. Does not send messages, publish, restore or restart."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-url', help='Full HTTPS panel URL including public prefix')
    args = parser.parse_args()
    import config
    import db
    import httpx
    import service_audit
    import update_manager
    from services.xui_api import request_json_sync, fetch_control_snapshot_sync, fetch_snapshot_sync
    checks = []
    def check(name, action):
        try:
            detail = action()
            checks.append({'check': name, 'ok': True, 'detail': detail})
        except Exception as error:
            # Redact known credentials even when a third-party error contains a URL.
            detail = str(error)
            for key in ('BOT_TOKEN', 'MASTER_API_TOKEN', 'XUI_PASSWORD', 'GITHUB_API_TOKEN', 'DATABASE_URL', 'WEB_PASSWORD'):
                value = str(getattr(config, key, '') or '')
                if value: detail = detail.replace(value, '[REDACTED]')
            checks.append({'check': name, 'ok': False, 'detail': detail[:1000]})
    def versions():
        version_files = [ROOT / 'VERSION', ROOT / 'app' / 'VERSION', ROOT / 'static' / 'VERSION', ROOT / 'app' / 'static' / 'VERSION']
        existing = [path for path in version_files if path.is_file()]
        values = [path.read_text().strip() for path in existing]
        if len(values) < 1: raise RuntimeError('VERSION not found')
        if len(set(values)) != 1: raise RuntimeError('Version copies mismatch: ' + repr(values))
        if not __import__('re').fullmatch(r'\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?', values[0]): raise RuntimeError('Invalid VERSION: ' + repr(values[0]))
        return values[0]
    check('version', versions)
    def database():
        with db.connect() as connection:
            assert connection.execute('SELECT 1').fetchone()[0] == 1
            return {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                    for table in ('users', 'payments', 'user_events', 'panel_push_subscriptions')}
    check('PostgreSQL read', database)
    def units():
        report = service_audit.audit()
        if not report.get('healthy'): raise RuntimeError(json.dumps(report, ensure_ascii=False))
        return 'No duplicate launchers detected'
    check('services / duplicate launchers', units)
    def control():
        data = fetch_control_snapshot_sync(force=True)
        if data.get('error') or not data.get('status'): raise RuntimeError(data.get('error') or 'Empty status')
        return {'status': data['status'], 'nodes': len(data.get('nodes') or [])}
    check('3x-ui status', control)
    check('3x-ui inbounds', lambda: len(request_json_sync('GET', 'panel/api/inbounds/list').get('obj') or []))
    def clients():
        data = fetch_snapshot_sync(force=True)
        if data.get('stale'): raise RuntimeError(data.get('error') or 'Stale client snapshot')
        return {'clients': len(data.get('clients') or []), 'online': len(data.get('online') or [])}
    check('3x-ui clients and online', clients)
    if args.public_url:
        base = args.public_url.rstrip('/')
        if not base.startswith('https://'): raise SystemExit('--public-url must use HTTPS')
        def service_worker():
            response = httpx.get(base + '/service-worker.js', timeout=15, follow_redirects=False, trust_env=False)
            response.raise_for_status()
            assert 'javascript' in response.headers.get('content-type', '')
            assert response.headers.get('service-worker-allowed')
            expected = versions()
            assert f"const VERSION = '{expected}';" in response.text
            return {'status': response.status_code, 'scope': response.headers['service-worker-allowed']}
        check('public HTTPS / SW version and scope', service_worker)
    def github():
        if not update_manager.publisher_enabled(): return 'Client role; publishing unavailable'
        response = update_manager.github_request('GET', '/repos/' + update_manager.github_owner() + '/' + update_manager.github_repo() + '/git/ref/heads/main')
        response.raise_for_status()
        sha = (response.json().get('object') or {}).get('sha')
        if not sha: raise RuntimeError('GitHub did not return main SHA')
        return {'main_sha': sha, 'write_permissions': 'Must verify by publishing via the panel'}
    check('GitHub main read', github)
    print(json.dumps({'version': update_manager.current_version(), 'checks': checks}, ensure_ascii=False, indent=2))
    return int(any(not item['ok'] for item in checks))

if __name__ == '__main__':
    raise SystemExit(main())
