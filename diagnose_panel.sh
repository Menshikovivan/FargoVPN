#!/usr/bin/env bash
set -u
umask 077
OUT="/tmp/fargovpn_panel_diag_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$OUT") 2>&1
run(){ echo; echo ">>> $*"; "$@" 2>&1 || true; }
echo "=== FargoVPN / 3x-ui network diagnostic ==="
echo "time: $(date -Is)"
echo "host: $(hostname -f 2>/dev/null || hostname)"
run ss -lntup
run systemctl status vpn-service-web.service --no-pager -l
run systemctl status vpn-service-nginx-guard.service --no-pager -l
run systemctl status nginx.service --no-pager -l
run nginx -t
run bash -lc 'nginx -T 2>&1 | grep -nE "listen |server_name |stream \{|ssl_preread|proxy_pass|fargovpn|01-main|nginx-http.sock|limit_req" | head -n 600'
run systemctl status x-ui.service --no-pager -l
if [[ -f /etc/x-ui/x-ui.db ]]; then run bash -lc 'sqlite3 /etc/x-ui/x-ui.db "select key,value from settings where key in ('"'"'webPort'"'"','"'"'webBasePath'"'"','"'"'webListen'"'"','"'"'subPort'"'"','"'"'subListen'"'"');"'; fi
run ufw status verbose
run nft list ruleset
run iptables -S
run iptables -t nat -S
run fail2ban-client status
run free -h
run df -h
run bash -lc 'sysctl net.ipv4.tcp_max_syn_backlog net.core.somaxconn fs.file-max 2>/dev/null || true; ulimit -n; ps -eo pid,ppid,etimes,%cpu,%mem,rss,cmd --sort=-%mem | head -n 30'
run bash -lc 'SOCKET=$(python3 -c "import config; print(getattr(config, \"WEB_SOCKET_PATH\", \"/run/vpn-service/fargovpn.sock\"))"); test -S "$SOCKET" && echo "FargoVPN UDS: $SOCKET" && curl --unix-socket "$SOCKET" -fsS --max-time 10 http://localhost/health || true'
DOMAIN="${WEB_DOMAIN:-${1:-}}"
if [[ -n "$DOMAIN" ]]; then
  run curl -4 -vk --connect-timeout 5 --max-time 15 "https://${DOMAIN}/health"
  run getent ahosts "$DOMAIN"
else
  echo "DOMAIN not supplied; skipping public host health check. Set WEB_DOMAIN or pass it as argument."
fi
run journalctl -u vpn-service-web.service --since '-45 min' --no-pager -n 500
run journalctl -u nginx.service --since '-45 min' --no-pager -n 500
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "$APP_DIR/config.py" ]]; then
  echo; echo '>>> CONFIG (SELECTED KEYS ONLY)'
  grep -E '^(WEB_HOST|WEB_SOCKET_PATH|WEB_REVERSE_PROXY|WEB_PUBLIC_PREFIX|WEB_DOMAIN|WEB_TLS_SERVER_NAME|WEB_COOKIE_HTTPS_ONLY|WEB_TRUST_PROXY_HEADERS)[[:space:]]*=' "$APP_DIR/config.py" | sed -E 's/(WEB_PUBLIC_PREFIX[[:space:]]*=).*/\1 <configured>/; s/(WEB_DOMAIN[[:space:]]*=).*/\1 <configured>/; s/(WEB_TLS_SERVER_NAME[[:space:]]*=).*/\1 <configured>/'
fi
echo; echo "REPORT=$OUT"; echo '=== end ==='
