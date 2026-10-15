<?php
// Installs the WooCommerce REST key that the woo-mcp pod uses. The key/secret come from the
// Kubernetes Secret (never hardcoded); WooCommerce stores only an HMAC of the key.
global $wpdb;
$ck = getenv('WC_CONSUMER_KEY'); $cs = getenv('WC_CONSUMER_SECRET');
if (!$ck || !$cs) { fwrite(STDERR, "missing WC_CONSUMER_KEY/SECRET\n"); exit(1); }
$t = $wpdb->prefix . 'woocommerce_api_keys';
if ($wpdb->get_var("SHOW TABLES LIKE '$t'") !== $t) { fwrite(STDERR, "WooCommerce tables not ready\n"); exit(1); }
$wpdb->delete($t, ['description' => 'mcp-gateway']);
$ok = $wpdb->insert($t, [
  'user_id' => 1, 'description' => 'mcp-gateway', 'permissions' => 'read_write',
  'consumer_key' => wc_api_hash($ck), 'consumer_secret' => $cs, 'nonces' => '', 'truncated_key' => substr($ck, -7),
]);
if (!$ok) { fwrite(STDERR, "could not insert API key\n"); exit(1); }
echo "REST API key installed\n";
