"""WooCommerce MCP server (Streamable HTTP).

Cluster-internal only: exposed via a ClusterIP Service and a NetworkPolicy that
admits traffic from the gateway pods only. A shared bearer token (MCP_AUTH_TOKEN)
is an additional layer; the gateway holds it, members never see it.
"""
from __future__ import annotations

import hmac
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings, load_settings
from .wc_client import WooClient, WooError

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]


def _product(p: dict) -> dict:
    return {
        "id": p.get("id"), "name": p.get("name"), "price": p.get("price"),
        "regular_price": p.get("regular_price"), "status": p.get("status"),
        "stock_status": p.get("stock_status"), "sku": p.get("sku"),
        "permalink": p.get("permalink"),
    }


def _order_summary(o: dict) -> dict:
    return {
        "id": o.get("id"), "status": o.get("status"), "total": o.get("total"),
        "currency": o.get("currency"), "date_created": o.get("date_created"),
        "customer": " ".join(filter(None, [o.get("billing", {}).get("first_name"),
                                           o.get("billing", {}).get("last_name")])) or None,
        "items": [{"name": i.get("name"), "quantity": i.get("quantity")} for i in o.get("line_items", [])],
    }


def build_mcp(settings: Settings, client: WooClient) -> FastMCP:
    mcp = FastMCP(
        "woocommerce",
        instructions="Manage a WooCommerce store: products and orders.",
        host="0.0.0.0",
        port=settings.port,
        # Stateless + JSON responses: any replica can serve any request, which
        # keeps the gateway's upstream connections simple.
        stateless_http=True,
        json_response=True,
        # Host-header (DNS rebinding) checks only make sense for localhost
        # servers; here access control is NetworkPolicy + bearer token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    tz = ZoneInfo(settings.store_timezone)

    @mcp.tool()
    async def list_products(search: str | None = None, status: str = "any",
                            per_page: int = 20, page: int = 1) -> list[dict]:
        """List products. Optional text search; status: any|publish|draft|private."""
        params = {"per_page": max(1, min(per_page, 100)), "page": page, "status": status}
        if search:
            params["search"] = search
        return [_product(p) for p in await client.request("GET", "/products", params=params)]

    @mcp.tool()
    async def create_product(name: str, regular_price: str, description: str = "",
                             status: Literal["publish", "draft"] = "publish") -> dict:
        """Create a simple product. regular_price is a decimal string like "19.99"."""
        try:
            if float(regular_price) < 0:
                raise ValueError
        except ValueError:
            raise WooError("regular_price must be a non-negative decimal string, e.g. '19.99'")
        body = {"name": name, "type": "simple", "regular_price": regular_price,
                "description": description, "status": status}
        return _product(await client.request("POST", "/products", json=body))

    @mcp.tool()
    async def list_orders(status: str = "any", today_only: bool = False,
                          after: str | None = None, before: str | None = None,
                          per_page: int = 20, page: int = 1) -> list[dict]:
        """List orders. today_only=true limits to orders created today (store timezone).
        after/before accept ISO-8601 datetimes."""
        params: dict = {"per_page": max(1, min(per_page, 100)), "page": page, "status": status}
        if today_only:
            # Midnight in the store's timezone, sent as UTC. WooCommerce reads a bare `after` in the WordPress site
            # timezone (UTC unless set), so a local midnight sent as-is would drop orders placed between local
            # midnight and UTC midnight (00:00-05:30 in India).
            start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(timezone.utc)
            params["after"] = start.strftime("%Y-%m-%dT%H:%M:%S")
            params["dates_are_gmt"] = "true"
        elif after:
            params["after"] = after
        if before:
            params["before"] = before
        return [_order_summary(o) for o in await client.request("GET", "/orders", params=params)]

    @mcp.tool()
    async def get_order(order_id: int) -> dict:
        """Get full details of one order by id."""
        o = await client.request("GET", f"/orders/{int(order_id)}")
        d = _order_summary(o)
        d["payment_method"] = o.get("payment_method_title")
        d["shipping_total"] = o.get("shipping_total")
        return d

    @mcp.tool()
    async def update_order_status(order_id: int, status: OrderStatus) -> dict:
        """Change an order's status."""
        return _order_summary(await client.request("PUT", f"/orders/{int(order_id)}", json={"status": status}))

    @mcp.custom_route("/healthz", methods=["GET"])  # liveness: process is up
    async def healthz(_: Request):
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])  # readiness: store reachable
    async def readyz(_: Request):
        try:
            await client.ping()
            return JSONResponse({"status": "ready"})
        except WooError as e:
            return JSONResponse({"status": "not-ready", "reason": str(e)}, status_code=503)

    return mcp


class BearerAuth:
    """ASGI middleware: require `Authorization: Bearer <token>` except on health probes."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and scope["path"] not in ("/healthz", "/readyz"):
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, client: WooClient | None = None):
    settings = settings or load_settings()
    client = client or WooClient(settings.wc_base_url, settings.wc_consumer_key,
                                 settings.wc_consumer_secret, settings.http_timeout,
                                 auth_mode=settings.wc_auth_mode, forwarded_proto=settings.wc_forwarded_proto)
    mcp = build_mcp(settings, client)
    return BearerAuth(mcp.streamable_http_app(), settings.mcp_auth_token)


app = None  # created lazily by `uvicorn app.server:factory --factory`


def factory():
    return create_app()
