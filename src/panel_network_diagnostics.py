#!/usr/bin/env python3
"""Read-only panel incident collector. Run inside the installed app directory.

No firewall changes, no restarts, no credentials/config dumps. Local HTTPS
health checks bypass certificate verification only on loopback.
"""
from __future__ import annotations
import argparse
import json
import re
import socket
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path


def redact(text: str) -> str:
    text = re.sub(r'bot\d{5,}:[A-Za-z0-9_-]+', 'bot[REDACTED]', text)
    text = re.sub(r'(?i)(authorization[=: ]+)([^\r\n]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:token|password|secret|api_key)[=: ]+)([^\s,;]+)', r'\1[REDACTED]', text)
    # URLs can contain subscription credentials or secret 3x-ui paths.
    return re.sub(r'(https?://[^/\s"\']+)/[^\s"\']*', r'\1/[REDACTED-PATH]', text)


def command(args: list[str]) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=8, check=False)
        return {'code': result.returncode, 'output': redact((result.stdout + result.stderr)[-24000:])}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': str(error)}


def health(port: int, scheme: str) -> dict:
    started = time.monotonic()
    try:
        context = ssl._create_unverified_context() if scheme == 'https' else None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(f'{scheme}://127.0.0.1:{port}/health', timeout=3) as response:
            return {'status': response.status, 'body': response.read(300).decode(errors='replace'), 'seconds': round(time.monotonic()-started, 3)}
    except Exception as error:
        return {'error': str(error), 'seconds': round(time.monotonic()-started, 3)}


def collect(host: str, port: int) -> dict:
    result = {'time_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'host': host, 'port': port}
    result['local_http'] = health(port, 'http')
    result['local_https'] = health(port, 'https')
    try:
        result['dns'] = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}) if host else []
    except OSError as error:
        result['dns'] = {'error': str(error)}
    checks = {
        'listener': ['ss', '-ltnp', f'sport = :{port}'],
        'web_service': ['systemctl', 'show', 'vpn-service-web', '--property=ActiveState,SubState,MainPID,NRestarts,MemoryCurrent,TasksCurrent,Result'],
        'nginx_check': ['nginx', '-t'],
        'ufw': ['ufw', 'status', 'numbered'],
        'nftables': ['nft', 'list', 'ruleset'],
        'iptables': ['iptables', '-S'],
        'fail2ban': ['fail2ban-client', 'status'],
        'memory': ['free', '-m'],
    }
    result['checks'] = {name: command(args) for name, args in checks.items()}
    # Inspect the effective config rather than assuming the repository template
    # is identical to the running server. Do not return full config or secrets.
    nginx = command(['nginx', '-T'])
    result['nginx_routing'] = [line.strip() for line in nginx.get('output', '').splitlines()
                               if re.match(r'\s*(listen|server_name|limit_req|limit_conn|real_ip_header|set_real_ip_from|proxy_protocol)\b', line)]
    result['ipv6'] = command(['sysctl', 'net.ipv6.conf.all.disable_ipv6'])
    jails = result['checks']['fail2ban'].get('output', '')
    match = re.search(r'Jail list:\s*(.*)', jails)
    if match:
        result['jails'] = {jail: command(['fail2ban-client', 'status', jail])
                           for jail in match[1].split(', ') if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', jail)}
    result['interpretation'] = (
        'Local /health=200 while remote access times out: inspect DNS/AAAA, firewall bans, provider filtering and the VPN route. '
        'Local /health hangs too: inspect application load and event-loop blocking. '
        'HTTP fails but HTTPS=200 is normal for a TLS listener. HTTP 429 is an application response, not a TCP timeout.'
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='')
    parser.add_argument('--port', type=int)
    parser.add_argument('--watch', type=int, default=0, help='Number of additional lightweight samples (max 360)')
    parser.add_argument('--interval', type=int, default=10)
    args = parser.parse_args()
    try:
        import config
        socket_path = str(getattr(config, 'WEB_SOCKET_PATH', '/run/vpn-service/fargovpn.sock') or '/run/vpn-service/fargovpn.sock')
        host = args.host or str(getattr(config, 'WEB_DOMAIN', ''))
    except ImportError:
        socket_path, host = '/run/vpn-service/fargovpn.sock', args.host
    report = collect(host, 443)
    report['web_socket_path'] = socket_path
    report['web_socket_exists'] = Path(socket_path).exists()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for _ in range(max(0, min(args.watch, 360))):
        time.sleep(max(5, min(args.interval, 60)))
        print(json.dumps({'time_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                          'socket_exists': Path(socket_path).exists()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
