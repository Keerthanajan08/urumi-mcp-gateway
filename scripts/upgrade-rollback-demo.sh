#!/usr/bin/env bash
# Upgrades and rollback with Helm (stand-out 9). Same chart, same command; only values/images change.
#
#   ./scripts/upgrade-rollback-demo.sh                       # gateway config + (optional) new images
#   WC=10.4.3 WP_IMAGE=wordpress:6.8-apache ./scripts/upgrade-rollback-demo.sh   # also upgrade the store
#   WOO_IMAGE=urumi/woo-mcp:v2 GW_IMAGE=urumi/gateway:v2 ./scripts/upgrade-rollback-demo.sh
#
# What it shows
#  1. good upgrade (store + MCP server + gateway): pre-upgrade DB dump, rolling update, store converges to new versions
#  2. bad upgrade (gateway image that does not exist): --atomic rolls back by itself; old pods never stopped serving
#  3. helm rollback to the revision before step 1: config/images return; store DB dump available if WooCommerce migrated
set -euo pipefail
NS=${NS:-urumi}; CHART=${CHART:-deploy/charts/urumi}; VALS=${VALS:-$CHART/values-local.yaml}
EXTRA=${EXTRA:-}   # extra flags, e.g. "-f my-overrides.yaml"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
store_versions() {
  kubectl -n "$NS" exec deploy/wordpress -c wp-setup -- sh -c \
    'echo "  WordPress core $(wp core version)  WooCommerce $(wp plugin get woocommerce --field=version)"' 2>/dev/null || echo "  (store not ready)"
}
state() {
  helm history urumi -n "$NS" --max 6
  echo "  gateway RATE_LIMIT_PER_MINUTE=$(kubectl -n "$NS" get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="RATE_LIMIT_PER_MINUTE")].value}')" \
       " gateway image=$(kubectl -n "$NS" get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].image}')" \
       " woo-mcp image=$(kubectl -n "$NS" get deploy woo-mcp -o jsonpath='{.spec.template.spec.containers[0].image}')"
  store_versions
}
wait_store() { kubectl -n "$NS" rollout status deploy/wordpress --timeout=900s >/dev/null; }

REV0=$(helm status urumi -n "$NS" -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['version'])")
say "0) current state (revision $REV0)"; state

say "1) good upgrade: gateway config${WC:+, WooCommerce $WC}${WP_IMAGE:+, WordPress image $WP_IMAGE}${WOO_IMAGE:+, woo-mcp $WOO_IMAGE}${GW_IMAGE:+, gateway $GW_IMAGE}"
set -x
helm upgrade urumi "$CHART" -n "$NS" -f "$VALS" $EXTRA --reuse-values --set gateway.rateLimitPerMinute=240 \
  ${WC:+--set store.woocommerceVersion=$WC} ${WP_IMAGE:+--set images.wordpress=$WP_IMAGE} \
  ${WOO_IMAGE:+--set images.wooMcp=$WOO_IMAGE} ${GW_IMAGE:+--set images.gateway=$GW_IMAGE} --atomic --wait --timeout 15m
set +x
wait_store; state
echo "  pre-upgrade backups:"; ./scripts/restore-store.sh 2>/dev/null | grep store- || true

say "2) bad upgrade: gateway image that cannot be pulled. --atomic waits, fails, and rolls back on its own
   (the automatic rollback reuses --timeout and runs the pre-rollback DB dump first, so do not make it too short)"
helm upgrade urumi "$CHART" -n "$NS" -f "$VALS" $EXTRA --reuse-values --set images.gateway=urumi/gateway:does-not-exist \
  --atomic --wait --timeout 2m && echo "unexpected: upgrade succeeded" || echo "  -> upgrade failed and was rolled back automatically"
kubectl -n "$NS" get pods -l app.kubernetes.io/component=gateway
state

say "3) manual rollback to revision $REV0 (the state before step 1)"
helm rollback urumi "$REV0" -n "$NS" --wait --timeout 15m
wait_store; state
cat <<'TXT'
  Code, config and images are back. Data on PVCs is untouched by Helm. If step 1 moved WooCommerce to a version
  that migrated the database, restore the dump taken just before it:
      ./scripts/restore-store.sh                 # list dumps
      ./scripts/restore-store.sh <file.sql.gz>   # restore (storefront is down for the duration)
TXT
helm test urumi -n "$NS"
