#!/usr/bin/env bash
# Scaling demo (stand-out 7): several gateway replicas behind one Service, no session affinity.
#  1) MCP traffic is spread across replicas and every request works (stateless Streamable HTTP).
#  2) The per-user rate limit is ONE budget shared by all replicas (Postgres counter), not N budgets.
# Usage: KEY=umk_... [APP=http://app.urumi.localtest.me] [REPLICAS=3] ./scripts/scale-demo.sh
set -euo pipefail
NS=${NS:-urumi}; APP=${APP:-http://app.urumi.localtest.me}; REPLICAS=${REPLICAS:-3}; : "${KEY:?set KEY to an API key}"
LIMIT=$(kubectl -n "$NS" get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="RATE_LIMIT_PER_MINUTE")].value}')

kubectl -n "$NS" scale deploy/gateway --replicas="$REPLICAS"
kubectl -n "$NS" rollout status deploy/gateway --timeout=180s
kubectl -n "$NS" get pods -l app.kubernetes.io/component=gateway -o wide

rpc() { curl -s -D /tmp/scale-h -o /tmp/scale-b -X POST "$APP/mcp" -H "Authorization: Bearer $KEY" \
          -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$1"
        printf '%s %s\n' "$(tr -d '\r' </tmp/scale-h | awk -F': ' 'tolower($1)=="x-served-by"{print $2}')" \
                          "$(python3 -c "import json;r=json.load(open('/tmp/scale-b'));print('rate-limited' if r.get('result',{}).get('isError') and 'Rate limit' in r['result']['content'][0]['text'] else ('error' if 'error' in r or r.get('result',{}).get('isError') else 'ok'))")"; }

echo; echo "--- 1) 30 tool calls, no sticky sessions: which replica served each, and did it work?"
for _ in $(seq 1 30); do rpc '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"utils__ping","arguments":{}}}'; done | sort | uniq -c

echo; echo "--- 2) shared rate limit: limit is ${LIMIT}/min per user. Sending $((LIMIT + 20)) more calls across $REPLICAS replicas."
echo "    per-replica limiting would allow $((LIMIT * REPLICAS)); a shared limit lets through about ${LIMIT} in this minute."
for _ in $(seq 1 $((LIMIT + 20))); do rpc '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"utils__ping","arguments":{}}}'; done \
  | awk '{print $2}' | sort | uniq -c
echo "(the 30 calls from step 1 count toward the same minute if it has not rolled over)"
echo; echo "Scale back with: kubectl -n $NS scale deploy/gateway --replicas=1   (or let helm upgrade restore gateway.replicas)"
