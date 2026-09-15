#!/usr/bin/env bash
set -u
umask 077
OUT="${1:-/tmp/fargovpn-forensic-$(date +%Y%m%d_%H%M%S).log}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec > >(tee -a "$OUT") 2>&1

echo "=== FargoVPN forensic diagnostic ==="
echo "timestamp=$(date -Is)"
echo "host=$(hostname -f 2>/dev/null || hostname)"
echo "app_dir=$APP_DIR"

echo; echo "=== VERSION ==="
cat "$APP_DIR/VERSION" 2>/dev/null || true

echo; echo "=== SYSTEM ==="
uptime || true
free -h || true
cat /proc/loadavg || true
df -h / || true

echo; echo "=== SERVICES ==="
for unit in vpn-service-bot.service vpn-service-web.service vpn-service-web.socket vpn-service-backup.timer vpn-service-reminders.timer; do
  echo "--- $unit ---"
  systemctl is-active "$unit" 2>/dev/null || true
  systemctl show "$unit" --property=MainPID,ActiveState,SubState,ExecMainStartTimestamp,MemoryCurrent,CPUUsageNSec 2>/dev/null || true
done

echo; echo "=== PROCESSES ==="
ps -eo pid,ppid,user,stat,pcpu,pmem,etime,cmd --sort=-pcpu | head -40 || true

echo; echo "=== SOCKETS ==="
ss -lntup 2>/dev/null | head -100 || true
ss -lx 2>/dev/null | grep -E 'fargovpn|vpn-service' || true

echo; echo "=== DATABASE ==="
if [[ -f "$APP_DIR/config.py" ]]; then
  DB_PATH=$(PYTHONPATH="$APP_DIR" python - <<'PY'
import config
print(getattr(config,'DB_PATH',''))
PY
)
  if [[ -n "$DB_PATH" && -f "$DB_PATH" ]]; then
    echo "db_path=$DB_PATH"
    PYTHONPATH="$APP_DIR" python - <<'PY'
import config, sqlite3, time
p=str(getattr(config,'DB_PATH',''))
try:
  c=sqlite3.connect(p, timeout=3)
  print('journal_mode=', c.execute('PRAGMA journal_mode').fetchone()[0])
  print('busy_timeout=', c.execute('PRAGMA busy_timeout').fetchone()[0])
  print('quick_check=', c.execute('PRAGMA quick_check').fetchone()[0])
  t=time.monotonic(); c.execute('SELECT COUNT(*) FROM users').fetchone(); print('users_select_ms=', int((time.monotonic()-t)*1000))
  c.close()
except Exception as e: print('db_error=',type(e).__name__,str(e)[:300])
PY
  fi
fi

echo; echo "=== HTTP LOCAL HEALTH ==="
for url in "http://127.0.0.1:8088/health"; do
  echo "--- $url ---"
  curl -sS -o /dev/null -w 'http=%{http_code} connect=%{time_connect}s start=%{time_starttransfer}s total=%{time_total}s\n' --connect-timeout 2 --max-time 8 "$url" || true
done

echo; echo "=== NGINX ==="
nginx -t 2>&1 || true
if command -v nginx >/dev/null 2>&1; then
  nginx -T 2>&1 | grep -E 'server_name|listen 443|fargovpn|proxy_pass http://unix' | head -150 || true
fi

echo; echo "=== RECENT LOG SIGNALS ==="
for unit in vpn-service-web.service vpn-service-bot.service; do
  echo "--- $unit ---"
  journalctl -u "$unit" --since '-15 min' --no-pager -n 250 2>/dev/null | grep -E 'performance|ERROR|WARNING|Traceback|No item with that key|xui|backup|push|receipt' | tail -120 || true
done

echo
echo "REPORT=$OUT"
echo "=== end ==="

# Read-only external dependency probes. No write endpoint is called.
if [[ -f "$APP_DIR/config.py" ]]; then
  echo; echo "=== 3X-UI READ-ONLY LATENCY ==="
  PYTHONPATH="$APP_DIR" python - <<'PY' || true
import time
try:
    from services.xui_api import request_json_sync
    for method, path in (("GET", "panel/api/server/status"), ("GET", "panel/api/clients/list")):
        t=time.monotonic()
        try:
            data=request_json_sync(method, path, timeout=8.0)
            print(f"xui {method} {path} ms={int((time.monotonic()-t)*1000)} ok={bool(data.get('success'))}")
        except Exception as exc:
            print(f"xui {method} {path} ms={int((time.monotonic()-t)*1000)} error={type(exc).__name__}:{str(exc)[:240]}")
except Exception as exc:
    print(f"xui_probe_error={type(exc).__name__}:{str(exc)[:240]}")
PY
  echo; echo "=== YANDEX READ-ONLY CONNECTION ==="
  PYTHONPATH="$APP_DIR" python - <<'PY' || true
import time
try:
    from backup import test_yandex_connection
    t=time.monotonic()
    ok, detail=test_yandex_connection()
    print(f"yandex_connection_ms={int((time.monotonic()-t)*1000)} ok={ok} detail={str(detail)[:300]}")
except Exception as exc:
    print(f"yandex_probe_error={type(exc).__name__}:{str(exc)[:240]}")
PY
fi
