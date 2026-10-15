"""Configuration comes only from environment variables.

In Kubernetes the WC_* secrets are injected from a Secret (never baked into
the image or the chart values). See charts/woo-mcp.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    wc_base_url: str
    wc_consumer_key: str
    wc_consumer_secret: str
    # Optional shared bearer token. The gateway stores it as this server's
    # "credential" and sends it on every call (defence in depth on top of the
    # NetworkPolicy that already restricts who can reach this pod).
    mcp_auth_token: str
    store_timezone: str
    http_timeout: float
    port: int
    # WooCommerce only honours Basic-auth REST keys when it believes the request is HTTPS.
    # In-cluster we speak plain HTTP, so we either (a) send X-Forwarded-Proto: https (the chart
    # teaches wp-config.php to trust it) or (b) fall back to query-string keys.
    wc_auth_mode: str  # basic | query
    wc_forwarded_proto: str


def load_settings() -> Settings:
    return Settings(
        wc_base_url=os.environ.get("WC_BASE_URL", "http://localhost:8080").rstrip("/"),
        wc_consumer_key=os.environ.get("WC_CONSUMER_KEY", ""),
        wc_consumer_secret=os.environ.get("WC_CONSUMER_SECRET", ""),
        mcp_auth_token=os.environ.get("MCP_AUTH_TOKEN", ""),
        store_timezone=os.environ.get("STORE_TIMEZONE", "UTC"),
        http_timeout=float(os.environ.get("WC_HTTP_TIMEOUT", "15")),
        port=int(os.environ.get("PORT", "8000")),
        wc_auth_mode=os.environ.get("WC_AUTH_MODE", "basic"),
        wc_forwarded_proto=os.environ.get("WC_FORWARDED_PROTO", "https"),
    )
