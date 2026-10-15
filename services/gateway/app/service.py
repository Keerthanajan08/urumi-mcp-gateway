"""Gateway core: tool aggregation, routing, permissions, audit, rate limiting.

Both the external MCP endpoint (/mcp) and the dashboard chat agent call this one
service, so permissions, timeouts and audit behave identically for both.

Naming scheme (collision handling)
  exposed name = "<server-slug>__<upstream-tool-name>"
Slugs are unique ([a-z0-9-], <=24 chars), so two servers exposing `search` become
`notes__search` and `docs__search`. The prefix is always applied (not only on
collision) so names are stable when servers are added/removed. Names longer than
64 chars (a common client limit) are shortened with a short hash suffix; the
routing table maps the exposed name back to (server, original tool).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from mcp import types
from prometheus_client import Counter, Histogram
import logging
import random

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .models import AuditLog, RateCounter, Server, ToolPolicy
from .upstream import ServerCfg, UpstreamError, UpstreamManager

CALLS = Counter("gateway_tool_calls_total", "Tool calls", ["server", "tool", "outcome"])
LATENCY = Histogram("gateway_tool_call_seconds", "Tool call latency", ["server"])

ROLE_RANK = {"member": 0, "admin": 1}
SEP = "__"
MAX_NAME = 64
_BAD_CHARS = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")


@dataclass(frozen=True)
class Principal:
    id: int
    email: str
    role: str
    name: str = ""


@dataclass
class ToolEntry:
    name: str  # exposed (namespaced)
    slug: str
    server_name: str
    tool: str  # original upstream name
    description: str
    input_schema: dict


def exposed_name(slug: str, tool: str) -> str:
    name = f"{slug}{SEP}{tool}"
    if len(name) <= MAX_NAME:
        return name
    h = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{name[: MAX_NAME - 7]}_{h}"


def clean_text(s: str | None, limit: int) -> str:
    """Tool metadata is attacker-controlled input (see threat model): strip invisible /
    bidi / control characters used to hide instructions and cap its length."""
    return _BAD_CHARS.sub("", s or "")[:limit]


def _clean_schema(node, depth=0):
    if depth > 12:
        return node
    if isinstance(node, dict):
        return {k: (clean_text(v, 300) if k == "description" and isinstance(v, str) else _clean_schema(v, depth + 1))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_clean_schema(v, depth + 1) for v in node]
    return node


class RateLimiter:
    def __init__(self, per_minute: int):
        self.n, self.hits = per_minute, defaultdict(deque)

    def allow(self, key) -> bool:
        if self.n <= 0:
            return True
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True


class SharedRateLimiter:
    """Fixed 1-minute window counter in the gateway database, so the limit is per user across ALL
    replicas (the in-memory limiter gave each replica its own budget). One small upsert per call.
    If the database write fails we degrade to the per-replica limiter rather than block traffic."""

    def __init__(self, per_minute: int, sm):
        self.n, self.sm, self.local = per_minute, sm, RateLimiter(per_minute)

    async def allow(self, key) -> bool:
        if self.n <= 0:
            return True
        k, window = f"user:{key}", int(time.time() // 60)
        cond = (RateCounter.key == k, RateCounter.window == window)
        try:
            async with self.sm() as db:
                res = await db.execute(update(RateCounter).where(*cond).values(count=RateCounter.count + 1))
                if res.rowcount == 0:
                    try:
                        db.add(RateCounter(key=k, window=window, count=1))
                        await db.flush()
                    except IntegrityError:  # another replica inserted first
                        await db.rollback()
                        await db.execute(update(RateCounter).where(*cond).values(count=RateCounter.count + 1))
                count = await db.scalar(select(RateCounter.count).where(*cond))
                if random.random() < 0.01:  # opportunistic cleanup of old windows
                    await db.execute(delete(RateCounter).where(RateCounter.window < window - 5))
                await db.commit()
            return (count or 0) <= self.n
        except Exception:  # noqa: BLE001
            logging.getLogger("gateway.ratelimit").exception("shared limiter failed; using local limiter")
            return self.local.allow(key)


def text_result(msg: str, error=True) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=error)


class GatewayService:
    def __init__(self, settings: Settings, upstreams: UpstreamManager, sessionmaker):
        self.s, self.up, self.sm = settings, upstreams, sessionmaker
        self.limiter = SharedRateLimiter(settings.rate_limit_per_minute, sessionmaker)

    # ---- listing --------------------------------------------------------
    async def _servers_and_policies(self):
        async with self.sm() as db:
            servers = (await db.scalars(select(Server).where(Server.enabled.is_(True)).order_by(Server.id))).all()
            pols = (await db.scalars(select(ToolPolicy))).all()
        return servers, {(p.server_id, p.tool_name): p.min_role for p in pols}

    async def _ensure_fresh(self, servers):
        """Servers we have never probed get a bounded, concurrent refresh so the first
        listing after startup is not empty. Known servers are served from cache."""
        unknown = [s for s in servers if s.slug not in self.up.state or self.up.state[s.slug].status == "unknown"]
        if unknown:
            cfgs = [self.up.to_cfg(s) for s in unknown]
            await asyncio.gather(*(self.up.refresh(c) for c in cfgs), return_exceptions=True)

    async def _all_entries(self) -> tuple[list[tuple[ToolEntry, int, str | None]], dict[str, Server]]:
        servers, pols = await self._servers_and_policies()
        await self._ensure_fresh(servers)
        out = []
        for srv in servers:
            st = self.up.state.get(srv.slug)
            if not st or st.status != "connected":
                continue  # dead/unknown upstream: its tools are withheld, others unaffected
            for t in st.tools:
                e = ToolEntry(
                    name=exposed_name(srv.slug, t["name"]), slug=srv.slug, server_name=srv.name, tool=t["name"],
                    description=f"[{srv.name}] " + clean_text(t.get("description"), 1024),
                    input_schema=_clean_schema(t.get("inputSchema") or {"type": "object", "properties": {}}),
                )
                out.append((e, srv.id, pols.get((srv.id, t["name"]))))
        return out, {s.slug: s for s in servers}

    async def list_tools(self, user: Principal) -> list[ToolEntry]:
        entries, _ = await self._all_entries()
        rank = ROLE_RANK.get(user.role, 0)
        return [e for e, _sid, min_role in entries if min_role is None or rank >= ROLE_RANK.get(min_role, 1)]

    # ---- calling --------------------------------------------------------
    async def call_tool(self, user: Principal, name: str, args: dict | None, source: str) -> types.CallToolResult:
        t0 = time.monotonic()
        slug = tool = ""
        outcome, err = "success", None
        result: types.CallToolResult
        try:
            if not await self.limiter.allow(user.id):
                outcome, err = "denied", "rate limit exceeded"
                return text_result("Rate limit exceeded. Slow down and retry shortly.")
            entries, servers = await self._all_entries()
            match = next(((e, sid, mr) for e, sid, mr in entries if e.name == name), None)
            if not match:
                slug = name.split(SEP, 1)[0] if SEP in name else ""
                tool = name.split(SEP, 1)[1] if SEP in name else name
                st = self.up.state.get(slug) if slug in servers else None
                if st and st.status == "unreachable":
                    # Known, enabled server that is down: say so (and why) instead of a generic "unknown tool".
                    outcome, err = "unreachable", st.error or "unreachable"
                    return text_result(f"Upstream '{slug}' is unreachable ({st.error or 'no answer'}); "
                                       f"its tools are temporarily unavailable. Other servers are unaffected.")
                outcome, err = "not_found", "unknown or unavailable tool"
                return text_result(f"Tool '{name}' is not available (unknown tool, or its server is disabled).")
            entry, _sid, min_role = match
            slug, tool = entry.slug, entry.tool
            if min_role is not None and ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(min_role, 1):
                outcome, err = "denied", f"requires role {min_role}"
                return text_result(f"Tool '{name}' is not available to your role.")
            cfg: ServerCfg = self.up.to_cfg(servers[slug])
            try:
                res = await self.up.call(cfg, entry.tool, args or {})
            except UpstreamError as e:
                outcome, err = ("timeout" if e.kind == "timeout" else "unreachable"), e.message
                return text_result(e.message)
            if res.isError:
                outcome = "error"
                err = " ".join(c.text for c in res.content if isinstance(c, types.TextContent))[:500] or "tool error"
            result = res
            return result
        finally:
            dur = time.monotonic() - t0
            CALLS.labels(slug or "-", tool or "-", outcome).inc()
            if slug:
                LATENCY.labels(slug).observe(dur)
            await self._audit(user, source, slug, tool, int(dur * 1000), outcome, err)

    async def _audit(self, user, source, slug, tool, ms, outcome, err):
        try:
            async with self.sm() as db:
                db.add(AuditLog(user_id=user.id, user_email=user.email, source=source, server_slug=slug,
                                tool=tool, duration_ms=ms, outcome=outcome, error=(err or None)))
                await db.commit()
        except Exception:  # noqa: BLE001 - audit failure must be loud but must not mask the result
            import logging
            logging.getLogger("gateway.audit").exception("failed to write audit log")
