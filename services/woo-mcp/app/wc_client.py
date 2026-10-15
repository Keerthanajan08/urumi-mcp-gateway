"""Thin async client for the WooCommerce REST API (wc/v3).

Why REST (vs WP-CLI or direct SQL)?
- It is the supported public interface: validates input, fires WooCommerce
  hooks (stock, emails, order notes) exactly like the admin UI would.
- It works over the network, so this MCP pod does NOT need the WordPress
  filesystem or database credentials. Blast radius = one scoped API key.
- WP-CLI would require exec/shared volume access into the WordPress pod;
  direct SQL would bypass business logic and need DB credentials.
"""
from __future__ import annotations

import httpx


class WooError(Exception):
    """Raised with a short, model-friendly message (no secrets)."""


class WooClient:
    def __init__(self, base_url: str, key: str, secret: str, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None,
                 auth_mode: str = "basic", forwarded_proto: str = "https"):
        headers = {"X-Forwarded-Proto": forwarded_proto} if forwarded_proto else {}
        self._qs = {"consumer_key": key, "consumer_secret": secret} if auth_mode == "query" else {}
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/wp-json/wc/v3",
            auth=(key, secret) if auth_mode == "basic" else None,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kw):
        try:
            kw["params"] = {**kw.get("params", {}), **self._qs}
            r = await self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise WooError("WooCommerce did not respond in time") from e
        except httpx.HTTPError as e:
            raise WooError(f"Could not reach WooCommerce: {type(e).__name__}") from e
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except Exception:
                pass
            raise WooError(f"WooCommerce returned {r.status_code}: {msg or r.reason_phrase}")
        return r.json()

    async def ping(self) -> bool:
        await self.request("GET", "/system_status", params={"_fields": "environment"})
        return True
