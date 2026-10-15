#!/bin/sh
# Idempotent store bootstrap. Runs as a sidecar next to WordPress so it can share the
# WordPress volume without needing ReadWriteMany. The main container only turns Ready once
# /var/www/html/.setup-complete exists, so Ingress/MCP never see a half-installed store.
cd /var/www/html || exit 1
WP="wp --path=/var/www/html"
log() { echo "[wp-setup] $*"; }

run_setup() {
  if ! $WP core is-installed 2>/dev/null; then
    log "installing WordPress"
    $WP core install --url="$SITE_URL" --title="$SITE_TITLE" --admin_user="$WP_ADMIN_USER" \
      --admin_password="$WP_ADMIN_PASSWORD" --admin_email="$WP_ADMIN_EMAIL" --skip-email || return 1
  fi
  # Upgrades are declarative: the WordPress core on the PVC follows images.wordpress, and WooCommerce follows
  # store.woocommerceVersion. Changing either value and running `helm upgrade` converges the store here.
  IMAGE_WP=$(cat .image-wp-version 2>/dev/null || true)
  CORE=$($WP core version 2>/dev/null || true)
  if [ -n "$IMAGE_WP" ] && [ -n "$CORE" ] && [ "$IMAGE_WP" != "$CORE" ]; then
    log "WordPress core $CORE -> $IMAGE_WP (image changed)"
    $WP core update --version="$IMAGE_WP" --force || return 1
    $WP core update-db || return 1
  fi
  if ! $WP plugin is-installed woocommerce; then
    log "installing WooCommerce $WC_VERSION (needs outbound access to wordpress.org)"
    $WP plugin install woocommerce --version="$WC_VERSION" --activate || return 1
  fi
  HAVE=$($WP plugin get woocommerce --field=version 2>/dev/null || true)
  if [ -n "$WC_VERSION" ] && [ "$HAVE" != "$WC_VERSION" ]; then
    log "WooCommerce $HAVE -> $WC_VERSION"
    $WP plugin install woocommerce --version="$WC_VERSION" --force || return 1
    $WP wc update || true   # WooCommerce database migrations (no-op if none)
  fi
  $WP plugin is-active woocommerce || $WP plugin activate woocommerce || return 1
  $WP rewrite structure '/%postname%/' --hard || return 1
  $WP option update woocommerce_currency "$CURRENCY" || return 1
  $WP option update woocommerce_default_country "$COUNTRY" || return 1
  $WP option update woocommerce_coming_soon no
  $WP option update woocommerce_store_pages_only no
  $WP option update woocommerce_calc_taxes no
  $WP option update woocommerce_ship_to_countries disabled
  $WP option update woocommerce_enable_guest_checkout yes
  $WP option update woocommerce_onboarding_profile '{"skipped":true}' --format=json
  $WP wc tool run install_pages --user=1 >/dev/null 2>&1 || true
  $WP option update woocommerce_cod_settings \
    '{"enabled":"yes","title":"Cash on delivery","description":"Pay when your order arrives.","enable_for_methods":[],"enable_for_virtual":"yes"}' \
    --format=json || return 1
  $WP eval-file /scripts/create-api-keys.php || return 1
  return 0
}

rm -f .setup-complete
until [ -f wp-config.php ]; do log "waiting for wp-config.php"; sleep 3; done
until $WP db check >/dev/null 2>&1; do log "waiting for database"; sleep 3; done
until run_setup; do log "setup step failed, retrying in 10s"; sleep 10; done
touch .setup-complete
log "store is ready"
while true; do sleep 3600; done
