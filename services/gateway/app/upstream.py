"""Connections to upstream MCP servers (Streamable HTTP).

Design
- One short-lived MCP session per operation (initialize -> request -> close).
  Upstreams are stateless from our side, so there is no long-lived connection to
  go stale, and N gateway replicas need no shared session state.
- Every operation is bounded by a timeout and isolated: a slow/dead upstream can
  only fail its own call. Listing runs all upstreams concurrently, each with its
  own timeout, and serves from a cache of the last successful listing.
- A background loop refreshes status + tools every HEALTH_INTERVAL seconds; a
  failed call also flips the server to "unreachable" immediately.
- Credentials are decrypted just-in-time, only into the outbound request headers.
- Redirects are disabled (SSRF), and the URL is re-validated before connecting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from prometheus_client import Gauge
from sqlalchemy import select

from .config import Settings
from .models import Server
from .security import Vault
from .ssrf import SSRFError, validate_upstream_url

log = logging.getLogger("gateway.upstream")
UP = Gauge("gateway_upstream_up", "1 if the upstream answered its last probe", ["server"])
TOOLS = Gauge("gateway_upstream_tools", "Tools exposed by the upstream", ["server"])


@dataclass
class ServerCfg:
    id: int
    slug: str
    name: str
    url: str
    auth_type: str
    header_name: str | None
    credential: str | None
    enabled: bool

    def headers(self) -> dict[str, str]:
        if not self.credential or self.auth_type == "none":
            return {}
        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {self.credential}"}
        if self.auth_type == "header" and self.header_name:
            return {self.header_name: self.credential}
        return {}


@dataclass
class UpstreamState:
    status: str = "unknown"  # connected | unreachable | disabled | starting | unknown
    error: str | None = None
    tools: list[dict] = field(default_factory=list)  # last good listing (raw upstream names)
    checked_at: float = 0.0
    latency_ms: int | None = None
    starting_until: float = 0.0  # hosted servers: failures before this time mean "still booting", not "dead"


class UpstreamError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message  # kind: timeout | unreachable | blocked


def _root_cause(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from upstream"
    if isinstance(e, httpx.ConnectError):
        return "connection refused or host unreachable"
    if isinstance(e, httpx.TimeoutException):
        return "upstream timed out"
    return f"{type(e).__name__}: {str(e)[:200]}"


class UpstreamManager:
    def __init__(self, settings: Settings, vault: Vault, sessionmaker):
        self.s, self.vault, self.sm = settings, vault, sessionmaker
        self.state: dict[str, UpstreamState] = {}
        self._task: asyncio.Task | None = None
        self.first_check_done = False  # readiness: do not take traffic before we know which upstreams are up

    # ---- config loading -------------------------------------------------
    def to_cfg(self, row: Server) -> ServerCfg:
        cred = self.vault.decrypt(row.credential_enc) if row.credential_enc else None
        return ServerCfg(row.id, row.slug, row.name, row.url, row.auth_type, row.header_name, cred, row.enabled)

    async def load_cfgs(self, only_enabled=False) -> list[ServerCfg]:
        async with self.sm() as db:
            q = select(Server).order_by(Server.id)
            if only_enabled:
                q = q.where(Server.enabled.is_(True))
            return [self.to_cfg(r) for r in (await db.scalars(q)).all()]

    # ---- connection -----------------------------------------------------
    @asynccontextmanager
    async def _session(self, cfg: ServerCfg, timeout: float):
        try:
            await validate_upstream_url(cfg.url, self.s)
        except SSRFError as e:
            raise UpstreamError("blocked", str(e))
        client = httpx.AsyncClient(
            headers=cfg.headers(),
            timeout=httpx.Timeout(timeout, connect=self.s.upstream_connect_timeout),
            follow_redirects=False,
        )
        try:
            async with streamable_http_client(cfg.url, http_client=client) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session
        finally:
            await client.aclose()

    async def refresh(self, cfg: ServerCfg) -> UpstreamState:
        st = self.state.setdefault(cfg.slug, UpstreamState())
        if not cfg.enabled:
            st.status, st.error, st.tools, st.checked_at = "disabled", None, [], time.time()
            return st
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(self.s.upstream_list_timeout):
                async with self._session(cfg, self.s.upstream_list_timeout) as sess:
                    tools, cursor = [], None
                    while True:
                        res = await sess.list_tools(cursor=cursor) if cursor else await sess.list_tools()
                        tools += [t.model_dump(exclude_none=True) for t in res.tools]
                        cursor = res.nextCursor
                        if not cursor:
                            break
            st.status, st.error, st.tools = "connected", None, tools
            st.latency_ms = int((time.monotonic() - t0) * 1000)
        except UpstreamError as e:
            st.status, st.error = "unreachable", e.message
        except TimeoutError:
            st.status, st.error = "unreachable", f"timed out after {self.s.upstream_list_timeout:g}s"
        except Exception as e:  # noqa: BLE001 - classify and report, never crash the loop
            st.status, st.error = "unreachable", _root_cause(e)
        st.checked_at = time.time()
        if st.status == "unreachable" and st.checked_at < st.starting_until:
            st.status = "starting"
        elif st.status == "connected":
            st.starting_until = 0.0
        UP.labels(cfg.slug).set(1 if st.status == "connected" else 0)
        TOOLS.labels(cfg.slug).set(len(st.tools))
        return st

    async def call(self, cfg: ServerCfg, tool: str, args: dict) -> types.CallToolResult:
        timeout = self.s.upstream_call_timeout
        try:
            async with asyncio.timeout(timeout):
                async with self._session(cfg, timeout) as sess:
                    return await sess.call_tool(tool, args, read_timeout_seconds=None)
        except UpstreamError:
            raise
        except TimeoutError:
            raise UpstreamError("timeout", f"Upstream '{cfg.slug}' did not answer within {timeout:g}s")
        except Exception as e:  # noqa: BLE001
            reason = _root_cause(e)
            st = self.state.setdefault(cfg.slug, UpstreamState())
            st.status, st.error, st.checked_at = "unreachable", reason, time.time()
            raise UpstreamError("unreachable", f"Upstream '{cfg.slug}' is unreachable ({reason})")

    # ---- background health loop ----------------------------------------
    async def refresh_all(self) -> None:
        cfgs = await self.load_cfgs()
        live = {c.slug for c in cfgs}
        for slug in list(self.state):
            if slug not in live:
                del self.state[slug]
        await asyncio.gather(*(self.refresh(c) for c in cfgs), return_exceptions=True)

    async def _loop(self):
        started = time.monotonic()
        while True:
            try:
                await self.refresh_all()
                self.first_check_done = True
            except Exception:  # noqa: BLE001
                log.exception("health loop iteration failed")
            # Probe fast for a fresh pod's first 30 s: its very first connections can be refused while the CNI is
            # still adding the new pod IP to the upstreams' "allow from gateway" NetworkPolicies.
            fast = time.monotonic() - started < 30
            await asyncio.sleep(min(2.0, self.s.health_interval) if fast else self.s.health_interval)

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="upstream-health")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
