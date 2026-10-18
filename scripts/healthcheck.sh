#!/usr/bin/env bash
set -Eeuo pipefail

SOCKET_PATH="${FARGOVPN_SOCKET:-/run/vpn-service/fargovpn.sock}"
URL="http://localhost/healthz"

if [[ ! -S "$SOCKET_PATH" ]]; then
  echo "FAIL: FargoVPN socket not found: $SOCKET_PATH" >&2
  exit 1
fi

response="$(curl --silent --show-error --fail --unix-socket "$SOCKET_PATH" "$URL")"
[[ "$response" == OK* ]] || {
  echo "FAIL: unexpected health response: $response" >&2
  exit 1
}
printf '%s\n' "$response"
