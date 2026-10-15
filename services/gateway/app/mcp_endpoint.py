"""The gateway as an MCP server: one Streamable HTTP endpoint (/mcp) for all clients.

Auth: `Authorization: Bearer umk_...` (per-user API key, looked up by SHA-256 hash).
The client's token is NEVER forwarded upstream; upstream calls use the separate
credential stored (encrypted) for that server. See the threat model.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.responses import JSONResponse

from .models import ApiKey, now
from .oauth import authenticate_oauth_token
from . import toolsearch
from .security import hash_api_key
from .service import GatewayService, Principal

log = logging.getLogger("gateway.mcp")


async def authenticate_api_key(sm, token: str) -> Principal | None:
    if not token.startswith("umk_"):
        return None
    async with sm() as db:
        key = await db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
        if not key or key.revoked_at is not None or key.user.disabled:
            return None
        # throttle last_used writes to once a minute
        if key.last_used_at is None or now() - key.last_used_at.replace(tzinfo=timezone.utc) > timedelta(minutes=1):
            key.last_used_at = now()
            await db.commit()
        u = key.user
        return Principal(u.id, u.email, u.role, u.name)


class McpAuth:
    """ASGI wrapper: authenticate, stash the Principal in the scope, delegate."""

    def __init__(self, inner, sm, settings=None):
        self.inner, self.sm, self.settings = inner, sm, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        principal = None
        if token:
            principal = await (authenticate_oauth_token(self.sm, token) if token.startswith("umo_")
                               else authenticate_api_key(self.sm, token))
        if not principal:
            # MCP authorization spec: tell OAuth-capable clients where to start the browser login.
            hdr = 'Bearer realm="mcp-gateway"'
            if self.settings is not None:
                base = self.settings.public_url or "http://" + dict(scope["headers"]).get(b"host", b"localhost").decode()
                hdr += f', resource_metadata="{base}/.well-known/oauth-protected-resource"'
            resp = JSONResponse({"error": "invalid, expired or revoked credentials"}, status_code=401,
                                headers={"WWW-Authenticate": hdr})
            return await resp(scope, receive, send)
        scope["principal"] = principal
        await self.inner(scope, receive, send)


def build_mcp_app(svc: GatewayService, sm):
    server: Server = Server("urumi-mcp-gateway", instructions=(
        "Team MCP gateway. Tools are namespaced as <server>__<tool>."))

    def principal() -> Principal:
        return server.request_context.request.scope["principal"]

    def mode() -> str | None:
        req = server.request_context.request
        return req.headers.get("x-gateway-tools") or req.query_params.get("tools")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return await toolsearch.advertised_tools(svc, principal(), mode())

    @server.call_tool(validate_input=False)  # the upstream validates against its own schema
    async def _call(name: str, arguments: dict) -> types.CallToolResult:
        meta = await toolsearch.handle_meta(svc, principal(), name, arguments, "mcp")
        return meta if meta is not None else await svc.call_tool(principal(), name, arguments, "mcp")

    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True,  # stateless => any replica can serve any request
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return manager, McpAuth(manager.handle_request, sm, svc.s)
