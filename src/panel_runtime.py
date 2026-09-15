"""One authoritative listener/TLS policy for launcher and installer health checks."""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import config


def domain() -> str:
    explicit = str(getattr(config, 'WEB_DOMAIN', '') or getattr(config, 'WEB_TLS_SERVER_NAME', '')).strip()
    if explicit:
        return explicit.lower().rstrip('.')
    for key in ('PUBLIC_PANEL_URL', 'BOT_PANEL_URL'):
        value = str(getattr(config, key, '') or '').strip()
        if value:
            return (urlsplit(value).hostname or '').lower().rstrip('.')
    return ''


def settings() -> dict:
    host = str(getattr(config, "WEB_HOST", "127.0.0.1")).strip()
    socket_path = str(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock") or "").strip()
    ipaddress.ip_address(host)
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("FargoVPN backend must use loopback WEB_HOST")
    if not socket_path.startswith("/") or len(socket_path) > 240:
        raise ValueError("WEB_SOCKET_PATH must be an absolute Unix socket path")
    if not bool(getattr(config, "WEB_REVERSE_PROXY", True)):
        raise ValueError("FargoVPN public access must use the Nginx 443 reverse proxy")
    if not bool(getattr(config, "WEB_COOKIE_HTTPS_ONLY", True)):
        raise ValueError("WEB_COOKIE_HTTPS_ONLY must remain enabled behind HTTPS Nginx")
    return {
        "host": host,
        "socket_path": socket_path,
        "scheme": "https",
        "internal_scheme": "http",
        "cert": "",
        "key": "",
        "domain": domain(),
        "reverse_proxy": True,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--scheme', action='store_true')
    args = parser.parse_args()
    options = settings()
    if args.scheme:
        print(options['scheme'])
        return
    if args.check:
        print(json.dumps(options))
        return
    import uvicorn
    socket_path = options['socket_path']
    # In production systemd owns the listening Unix socket. This removes the
    # short "socket absent" window during web-service restarts: Nginx can keep
    # connecting to the socket while systemd activates/restarts this service.
    listen_pid = int(os.environ.get('LISTEN_PID', '0') or 0)
    listen_fds = int(os.environ.get('LISTEN_FDS', '0') or 0)
    if listen_pid == os.getpid() and listen_fds >= 1:
        config = uvicorn.Config('webapp:app', fd=3, workers=1,
                    proxy_headers=True, forwarded_allow_ips='127.0.0.1,::1',
                    timeout_keep_alive=30, timeout_graceful_shutdown=10)
    else:
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            raise RuntimeError(f'WEB_SOCKET_PATH points to a directory: {socket_path}')
        config = uvicorn.Config('webapp:app', uds=socket_path, workers=1,
                    proxy_headers=True, forwarded_allow_ips='127.0.0.1,::1',
                    timeout_keep_alive=30, timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    server.run()


if __name__ == '__main__':
    main()
