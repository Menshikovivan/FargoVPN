#!/usr/bin/env bash
set -euo pipefail

echo "=== FargoVPN HTTPS / Nginx diagnostic ==="
echo "TLS is terminated by the existing Nginx HTTPS 443 frontend."
echo

printf '%-14s %-55s %-55s\n' SOURCE CERTIFICATE KEY
printf '%-14s %-55s %-55s\n' '--------------' '-------------------------------------------------------' '-------------------------------------------------------'

emit() {
  local source="$1" cert="$2" key="$3"
  [[ -f "$cert" ]] || return 0
  local end subject san
  end=$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null | sed 's/^notAfter=//') || return 0
  subject=$(openssl x509 -in "$cert" -noout -subject 2>/dev/null | sed 's/^subject=//') || true
  san=$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null | sed -n '2p' | sed 's/^[[:space:]]*//') || true
  printf '%-14s %-55s %-55s\n' "$source" "$cert" "$key"
  echo "  expires : $end"
  echo "  subject : $subject"
  echo "  SAN     : ${san:-not readable}"
  if [[ -f "$key" ]] && \
     openssl x509 -in "$cert" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum >/tmp/fargo-cert-pub.$$ && \
     openssl pkey -in "$key" -pubout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum >/tmp/fargo-key-pub.$$ && \
     cmp -s /tmp/fargo-cert-pub.$$ /tmp/fargo-key-pub.$$; then
    echo "  key/cert : MATCH"
  else
    echo "  key/cert : MISMATCH or unreadable"
  fi
  echo
}

for d in /etc/letsencrypt/live/*; do
  [[ -d "$d" ]] || continue
  emit "letsencrypt" "$d/fullchain.pem" "$d/privkey.pem"
done
for d in /root/.acme.sh/*; do
  [[ -d "$d" ]] || continue
  cert=""
  [[ -f "$d/fullchain.cer" ]] && cert="$d/fullchain.cer"
  [[ -z "$cert" && -f "$d/fullchain.pem" ]] && cert="$d/fullchain.pem"
  [[ -n "$cert" ]] || continue
  for key in "$d"/*.key; do
    [[ -f "$key" ]] || continue
    emit "acme.sh" "$cert" "$key"
  done
done

echo "=== Nginx 443 listeners / FargoVPN route ==="
if command -v nginx >/dev/null 2>&1; then
  nginx -t 2>&1 || true
  nginx -T 2>&1 | grep -nE 'listen 443|FARGOVPN|proxy_pass http://unix:|server_name ' | head -n 200 || true
else
  echo "nginx: not installed"
fi

echo
echo "=== FargoVPN backend ==="
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "$APP_DIR/config.py" ]]; then
  PYTHON="$APP_DIR/.venv/bin/python"
  [[ -x "$PYTHON" ]] || PYTHON=python3
  "$PYTHON" - <<'PY' || true
import config
print("WEB_HOST:", getattr(config, "WEB_HOST", ""))
print("WEB_SOCKET_PATH:", getattr(config, "WEB_SOCKET_PATH", ""))
print("WEB_REVERSE_PROXY:", getattr(config, "WEB_REVERSE_PROXY", False))
print("WEB_PUBLIC_PREFIX: <configured>" if getattr(config, "WEB_PUBLIC_PREFIX", "") else "WEB_PUBLIC_PREFIX: <missing>")
print("WEB_COOKIE_HTTPS_ONLY:", getattr(config, "WEB_COOKIE_HTTPS_ONLY", False))
PY
  SOCKET="$($PYTHON -c 'import config; print(getattr(config, "WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock"))')"
  if [[ -S "$SOCKET" ]] && curl --unix-socket "$SOCKET" -fsS --max-time 5 http://localhost/health; then
    echo "\nbackend health: OK"
  else
    echo "backend health: unavailable"
  fi
else
  echo "Application config not found at $APP_DIR/config.py"
fi

rm -f /tmp/fargo-cert-pub.$$ /tmp/fargo-key-pub.$$
echo "=== end ==="
