#!/usr/bin/env bash
# Quick end-to-end check of a running install. Usage: ADMIN_PW=... ./scripts/smoke.sh [domain]
set -euo pipefail
DOMAIN="${1:-urumi.localtest.me}"; APP="http://app.${DOMAIN}"; STORE="http://store.${DOMAIN}"
: "${ADMIN_PW:?set ADMIN_PW (kubectl -n urumi get secret urumi-secrets -o jsonpath='{.data.bootstrap-admin-password}' | base64 -d)}"
jar=$(mktemp); trap 'rm -f "$jar"' EXIT
echo "storefront:  $(curl -s -o /dev/null -w '%{http_code}' "$STORE/")"
curl -fsS -c "$jar" -H 'content-type: application/json' -d "{\"email\":\"${ADMIN_EMAIL:-admin@example.com}\",\"password\":\"$ADMIN_PW\"}" "$APP/api/auth/login" >/dev/null && echo "login:       ok"
echo "servers:"; curl -fsS -b "$jar" "$APP/api/servers" | python3 -c "import sys,json;[print('  ',s['slug'],s['status'],len(s['tools']),'tools') for s in json.load(sys.stdin)]"
KEY=$(curl -fsS -b "$jar" -H 'content-type: application/json' -d '{"name":"smoke"}' "$APP/api/keys" | python3 -c "import sys,json;print(json.load(sys.stdin)['key'])")
echo "mcp tools via gateway key:"
curl -fsS -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$APP/mcp" | python3 -c "import sys,json;[print('  ',t['name']) for t in json.load(sys.stdin)['result']['tools']]"
