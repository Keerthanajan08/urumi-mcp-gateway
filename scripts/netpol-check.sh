#!/usr/bin/env bash
# Is NetworkPolicy actually ENFORCED right now? (make netpol-check)
# Policies can exist without being enforced: plain kind has no enforcing CNI, and on k3d we have seen k3s's policy
# controller come back after a host/Docker restart without programming any rules. Run this before a demo.
# Fix if it fails on k3d:  docker restart k3d-urumi-server-0   (wait ~1 min, then run this again)
set -uo pipefail
NS=${NS:-urumi}; fail=0
probe() {  # namespace deployment host port expect(OPEN|BLOCKED)
  local got
  got=$(kubectl -n "$1" exec "deploy/$2" -c "$2" -- python -c "
import socket,sys
s=socket.socket(); s.settimeout(3)
try: s.connect(('$3',$4)); print('OPEN')
except Exception: print('BLOCKED')" 2>/dev/null | tail -1)
  if [ "$got" = "$5" ]; then printf '  ok    %-28s -> %-45s %s\n' "$1/$2" "$3:$4" "$got"
  else printf '  FAIL  %-28s -> %-45s %s (expected %s)\n' "$1/$2" "$3:$4" "${got:-no answer}" "$5"; fail=1; fi
}
echo "NetworkPolicy enforcement in namespace $NS:"
probe "$NS" woo-mcp   "mariadb.$NS.svc.cluster.local" 3306 BLOCKED
probe "$NS" woo-mcp   "postgres.$NS.svc.cluster.local" 5432 BLOCKED
probe "$NS" woo-mcp   "wordpress.$NS.svc.cluster.local" 80 OPEN
probe "$NS" notes-mcp "woo-mcp.$NS.svc.cluster.local" 8000 BLOCKED
probe "$NS" gateway   "mariadb.$NS.svc.cluster.local" 3306 BLOCKED
stores=$(kubectl get ns -l urumi.io/store -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
for s in $stores; do
  echo "Store namespace $s (cannot reach any other store's database or WordPress):"
  probe "$s" woo-mcp "wordpress.$s.svc.cluster.local" 80 OPEN
  probe "$s" woo-mcp "postgres.$NS.svc.cluster.local" 5432 BLOCKED
  probe "$s" woo-mcp "mariadb.$NS.svc.cluster.local" 3306 BLOCKED
  probe "$s" woo-mcp "wordpress.$NS.svc.cluster.local" 80 BLOCKED
  for o in $stores; do
    [ "$o" = "$s" ] && continue
    probe "$s" woo-mcp "mariadb.$o.svc.cluster.local" 3306 BLOCKED
    probe "$s" woo-mcp "wordpress.$o.svc.cluster.local" 80 BLOCKED
  done
  echo "Main store $NS -> store $s:"
  probe "$NS" woo-mcp "mariadb.$s.svc.cluster.local" 3306 BLOCKED
  probe "$NS" woo-mcp "wordpress.$s.svc.cluster.local" 80 BLOCKED
done
if [ $fail -ne 0 ]; then
  echo; echo "NetworkPolicies exist but are NOT being enforced (or are wrong)."
  echo "On k3d after a restart: docker restart k3d-urumi-server-0, wait a minute, run this again."
  exit 1
fi
echo "Enforced."
