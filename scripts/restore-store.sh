#!/usr/bin/env bash
# Restore the store database from a pre-upgrade dump (taken automatically by the chart's store-backup hook).
#   ./scripts/restore-store.sh               -> list available dumps
#   ./scripts/restore-store.sh <file.sql.gz> -> restore it (the storefront is down while it runs)
set -euo pipefail
NS=${NS:-urumi}; SECRET=${SECRET:-urumi-secrets}
IMG=$(kubectl -n "$NS" get sts mariadb -o jsonpath='{.spec.template.spec.containers[0].image}')
POLICY=$(kubectl -n "$NS" get sts mariadb -o jsonpath='{.spec.template.spec.containers[0].imagePullPolicy}')

run() {  # one-off pod with the backups PVC, labelled so the NetworkPolicy lets it reach MariaDB
  local overrides
  overrides=$(IMG="$IMG" POLICY="$POLICY" SECRET="$SECRET" CMD="$1" python3 -c '
import json, os
print(json.dumps({"spec": {
  "securityContext": {"runAsNonRoot": True, "runAsUser": 999, "fsGroup": 999, "seccompProfile": {"type": "RuntimeDefault"}},
  "volumes": [{"name": "b", "persistentVolumeClaim": {"claimName": "store-backups"}}],
  "containers": [{"name": "r", "image": os.environ["IMG"], "imagePullPolicy": os.environ["POLICY"],
                  "command": ["sh", "-c", os.environ["CMD"]],
                  "env": [{"name": "P", "valueFrom": {"secretKeyRef": {"name": os.environ["SECRET"], "key": "mariadb-root-password"}}}],
                  "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                  "volumeMounts": [{"name": "b", "mountPath": "/backups"}]}]}}))')
  kubectl -n "$NS" run "store-restore-$RANDOM" --rm -i --quiet --restart=Never --image="$IMG" \
    --labels=app.kubernetes.io/component=store-backup --overrides="$overrides"
}

if [ $# -eq 0 ]; then run 'ls -lht /backups'; exit 0; fi
case "$1" in */*|*..*) echo "give a file name from the listing"; exit 1;; esac
echo "stopping the storefront (WordPress) while the database is restored"
kubectl -n "$NS" scale deploy/wordpress --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app.kubernetes.io/component=wordpress --timeout=180s || true
trap 'kubectl -n "$NS" scale deploy/wordpress --replicas=1' EXIT     # always bring the store back
# retry the first connection: NetworkPolicy membership for a brand-new pod is programmed asynchronously
run "set -e; test -s /backups/$1; for i in \$(seq 1 30); do mariadb-admin ping -h mariadb -uroot -p\"\$P\" --silent && break; sleep 2; done
     gunzip -c /backups/$1 | mariadb -h mariadb -uroot -p\"\$P\"; echo restored $1"
kubectl -n "$NS" scale deploy/wordpress --replicas=1
trap - EXIT
kubectl -n "$NS" rollout status deploy/wordpress --timeout=600s
