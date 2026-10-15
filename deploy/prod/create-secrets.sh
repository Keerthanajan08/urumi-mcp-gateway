#!/usr/bin/env bash
# Creates the `urumi-secrets` Secret for production (values-prod.yaml has secrets.create=false).
# Run ONCE on a machine with kubectl access. Secrets never touch git or Helm values.
# Usage: LLM_API_KEY=... [ADMIN_PASSWORD=...] ./create-secrets.sh [namespace]
# (the first admin's EMAIL is a normal value: secrets.adminEmail in values-prod.yaml)
set -euo pipefail
NS="${1:-urumi}"; NAME="${SECRET_NAME:-urumi-secrets}"
r() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
WOO_TOK=$(r 32); NOTES_TOK=$(r 32); UTILS_TOK=$(r 32); PG=$(r 24); ADMIN_PW=${ADMIN_PASSWORD:-$(r 20)}
SEED=$(python3 - <<PY
import json
ns="$NS"
print(json.dumps([
 {"slug":"woocommerce","name":"WooCommerce Store","url":f"http://woo-mcp.{ns}.svc.cluster.local:8000/mcp","token":"$WOO_TOK"},
 {"slug":"notes","name":"Team Notes","url":f"http://notes-mcp.{ns}.svc.cluster.local:8000/mcp","token":"$NOTES_TOK"},
 {"slug":"utils","name":"Utilities","url":f"http://utils-mcp.{ns}.svc.cluster.local:8000/mcp","token":"$UTILS_TOK"}]))
PY
)
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
kubectl -n "$NS" create secret generic "$NAME" \
  --from-literal=mariadb-root-password="$(r 24)" \
  --from-literal=mariadb-password="$(r 24)" \
  --from-literal=wp-admin-password="$(r 20)" \
  --from-literal=wc-consumer-key="$(r 40)" \
  --from-literal=wc-consumer-secret="$(r 40)" \
  --from-literal=woo-mcp-token="$WOO_TOK" \
  --from-literal=notes-mcp-token="$NOTES_TOK" \
  --from-literal=utils-mcp-token="$UTILS_TOK" \
  --from-literal=postgres-password="$PG" \
  --from-literal=database-url="postgresql+asyncpg://urumi:${PG}@postgres:5432/gateway" \
  --from-literal=gateway-encryption-key="$(openssl rand -base64 32 | tr '+/' '-_')" \
  --from-literal=gateway-session-secret="$(r 48)" \
  --from-literal=bootstrap-admin-password="$ADMIN_PW" \
  --from-literal=llm-api-key="${LLM_API_KEY:-}" \
  --from-literal=seed-servers="$SEED"
echo "Created $NAME in $NS. Dashboard admin password: $ADMIN_PW   (store it in your password manager)"
