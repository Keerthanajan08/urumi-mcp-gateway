"""All configuration is environment-driven. Secrets arrive via Kubernetes Secrets
(mounted as env vars); nothing sensitive has a usable default in production."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _bool(v: str | None, default=False) -> bool:
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
    # Fernet key (urlsafe base64, 32 bytes). Encrypts upstream credentials at rest.
    encryption_key: str = ""
    # HMAC secret for dashboard session cookies.
    session_secret: str = ""
    session_ttl_seconds: int = 12 * 3600
    cookie_secure: bool = False

    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # Upstreams registered idempotently at startup (by slug; existing rows are left alone so
    # admin edits in the dashboard are never overwritten). JSON list of
    # {"slug","name","url","token"}; delivered from a Kubernetes Secret since it holds tokens.
    seed_servers: str = ""

    # Upstream behaviour
    upstream_connect_timeout: float = 5.0
    upstream_call_timeout: float = 30.0
    upstream_list_timeout: float = 8.0
    health_interval: float = 10.0

    # SSRF policy: private/cluster addresses are blocked unless the host matches
    # an allow pattern (fnmatch), e.g. "woo-mcp.*.svc.cluster.local".
    ssrf_allow_hosts: list[str] = field(default_factory=list)
    ssrf_allow_private: bool = False  # dev escape hatch only

    # Rate limit per API key / user (requests per minute); 0 disables.
    rate_limit_per_minute: int = 120

    # Public base URL of the dashboard host (used in OAuth metadata), e.g. https://app.example.com
    public_url: str = ""
    # Many-tools mode: "full" | "search" | "auto" (switch to search tools above the threshold)
    tools_mode: str = "auto"
    tool_search_threshold: int = 40
    # Hosted servers (container image -> Deployment in this namespace). Off unless the chart enables it.
    hosted_enabled: bool = False
    hosted_namespace: str = ""
    hosted_allowed_prefixes: list[str] = field(default_factory=list)  # image must start with one of these
    hosted_max_servers: int = 5
    hosted_run_as_user: int = 10001

    # Multi-store provisioning (bonus). Non-secret config rendered by Helm as JSON (see deploy/charts/urumi/values.yaml
    # `stores:`); the gateway only uses enabled/limits/naming, the provisioner uses all of it.
    stores: dict = field(default_factory=dict)

    # Chat agent (any OpenAI-compatible chat-completions endpoint)
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"
    chat_max_steps: int = 6
    chat_timeout: float = 60.0


def stores_cfg(s: "Settings") -> dict:
    """Store settings with defaults filled in."""
    d = {"enabled": False, "namespacePrefix": "store-", "maxPerUser": 2, "provisionTimeoutSeconds": 900,
         "domain": "urumi.localtest.me", "scheme": "http", "reservedSlugs": ["app", "store", "www", "gateway"]}
    d.update(s.stores or {})
    return d


def load_settings() -> Settings:
    e = os.environ.get
    return Settings(
        database_url=e("DATABASE_URL", Settings.database_url),
        encryption_key=e("GATEWAY_ENCRYPTION_KEY", ""),
        session_secret=e("GATEWAY_SESSION_SECRET", ""),
        session_ttl_seconds=int(e("SESSION_TTL_SECONDS", str(12 * 3600))),
        cookie_secure=_bool(e("COOKIE_SECURE")),
        bootstrap_admin_email=e("BOOTSTRAP_ADMIN_EMAIL", ""),
        bootstrap_admin_password=e("BOOTSTRAP_ADMIN_PASSWORD", ""),
        seed_servers=e("SEED_SERVERS", ""),
        upstream_connect_timeout=float(e("UPSTREAM_CONNECT_TIMEOUT", "5")),
        upstream_call_timeout=float(e("UPSTREAM_CALL_TIMEOUT", "30")),
        upstream_list_timeout=float(e("UPSTREAM_LIST_TIMEOUT", "8")),
        health_interval=float(e("HEALTH_INTERVAL", "10")),
        ssrf_allow_hosts=[h.strip() for h in e("SSRF_ALLOW_HOSTS", "").split(",") if h.strip()],
        ssrf_allow_private=_bool(e("SSRF_ALLOW_PRIVATE")),
        rate_limit_per_minute=int(e("RATE_LIMIT_PER_MINUTE", "120")),
        public_url=e("PUBLIC_URL", "").rstrip("/"),
        tools_mode=e("TOOLS_MODE", "auto"),
        tool_search_threshold=int(e("TOOL_SEARCH_THRESHOLD", "40")),
        hosted_enabled=_bool(e("HOSTED_ENABLED")),
        hosted_namespace=e("HOSTED_NAMESPACE", ""),
        hosted_allowed_prefixes=[h.strip() for h in e("HOSTED_ALLOWED_PREFIXES", "").split(",") if h.strip()],
        hosted_max_servers=int(e("HOSTED_MAX_SERVERS", "5")),
        hosted_run_as_user=int(e("HOSTED_RUN_AS_USER", "10001")),
        stores=json.loads(e("STORES_CONFIG", "") or "{}"),
        llm_base_url=e("LLM_BASE_URL", Settings.llm_base_url),
        llm_api_key=e("LLM_API_KEY", ""),
        llm_model=e("LLM_MODEL", Settings.llm_model),
        chat_max_steps=int(e("CHAT_MAX_STEPS", "6")),
        chat_timeout=float(e("CHAT_TIMEOUT", "60")),
    )
