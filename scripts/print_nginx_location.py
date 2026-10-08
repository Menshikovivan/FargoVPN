#!/usr/bin/env python3
"""Print the panel location for the EXTERNAL nginx owner; never write its config."""
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from security_policy import PANEL_CONTENT_SECURITY_POLICY
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import config
prefix = '/' + str(getattr(config, 'WEB_PUBLIC_PREFIX', '') or '').strip('/')
if prefix == '/' or not re.fullmatch(r'/[A-Za-z0-9/_-]+', prefix):
    raise SystemExit('Set a non-root WEB_PUBLIC_PREFIX before adding the external route')
prefix = prefix.rstrip('/') + '/'
print(f'''# Place inside the existing HTTPS server block (do not modify stream routing).
location = {prefix.rstrip('/')} {{ return 301 {prefix}; }}
location ^~ {prefix} {{
    # nginx >=1.29.3 with inherited add_header_inherit merge: add_header_inherit off;
    # Defining CSP here prevents inheritance of an incompatible parent CSP.
    # Keep the same policy as the application; an extra stricter policy intersects it.
    add_header Content-Security-Policy "{PANEL_CONTENT_SECURITY_POLICY}" always;
    proxy_pass http://unix:/run/vpn-service/fargovpn.sock:;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 90s;
    client_max_body_size 1024m;
}}
''')
