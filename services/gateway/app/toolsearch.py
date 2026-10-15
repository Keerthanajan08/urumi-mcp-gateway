"""Handling many tools: search -> describe -> call.

With ten servers the gateway can expose hundreds of tools, too many to put in a model's
context. In "search" mode tools/list returns only three meta-tools:

  gateway__search_tools(query, limit)   rank the caller's visible tools by keyword
  gateway__describe_tool(name)          full description + input schema of one tool
  gateway__call_tool(name, arguments)   execute it

`call_tool` delegates to GatewayService.call_tool, so role policies, upstream
credentials, timeouts, rate limit and audit (real server + real tool) are identical to a
direct call. The search index is built from the caller's own visible tools, so a member
can never discover or call an admin-only tool through it.

Mode selection: header `X-Gateway-Tools: search|full` or `?tools=` on /mcp, else
TOOLS_MODE (auto = search once visible tools exceed TOOL_SEARCH_THRESHOLD).
"""
from __future__ import annotations

import json
import re
import time

from mcp import types

from .service import GatewayService, Principal, text_result

SEARCH, DESCRIBE, CALL = "gateway__search_tools", "gateway__describe_tool", "gateway__call_tool"
META = {SEARCH, DESCRIBE, CALL}
_TOKEN = re.compile(r"[a-z0-9]+")


def meta_tools() -> list[types.Tool]:
    obj = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    return [
        types.Tool(name=SEARCH, description="[Gateway] Search the available tools by keyword. Use this first; "
                   "the full tool list is too large to show. Returns tool names and short descriptions.",
                   inputSchema=obj({"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}}, ["query"])),
        types.Tool(name=DESCRIBE, description="[Gateway] Get the full description and JSON input schema of one tool "
                   "found via search.", inputSchema=obj({"name": {"type": "string"}}, ["name"])),
        types.Tool(name=CALL, description="[Gateway] Execute a tool by exposed name with its arguments.",
                   inputSchema=obj({"name": {"type": "string"}, "arguments": {"type": "object"}}, ["name"])),
    ]


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def rank(entries, query: str, limit: int):
    q = set(_tokens(query))
    scored = []
    for e in entries:
        name_t, desc_t = set(_tokens(e.name)), set(_tokens(e.description))
        score = 3 * len(q & name_t) + len(q & desc_t) + (2 if any(t in e.name.lower() for t in q) else 0)
        if score:
            scored.append((score, e.name, e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [e for _, _, e in scored[:limit]]


async def advertised_tools(svc: GatewayService, user: Principal, mode: str | None) -> list[types.Tool]:
    entries = await svc.list_tools(user)
    mode = (mode or svc.s.tools_mode or "auto").lower()
    use_search = mode == "search" or (mode == "auto" and 0 < svc.s.tool_search_threshold < len(entries))
    if use_search:
        return meta_tools()
    return [types.Tool(name=e.name, description=e.description, inputSchema=e.input_schema) for e in entries]


async def handle_meta(svc: GatewayService, user: Principal, name: str, args: dict | None, source: str):
    """-> CallToolResult for a gateway__* meta tool, or None if `name` is not one."""
    if name not in META:
        return None
    args = args or {}
    if name == CALL:
        target = str(args.get("name", ""))
        if target in META:
            return text_result("Meta tools cannot be called through gateway__call_tool.")
        inner = args.get("arguments") or {}
        if not isinstance(inner, dict):
            return text_result("'arguments' must be an object.")
        return await svc.call_tool(user, target, inner, source)  # audited under the real server/tool

    t0 = time.monotonic()
    entries = await svc.list_tools(user)  # already filtered by the caller's role
    if name == SEARCH:
        limit = max(1, min(int(args.get("limit") or 8), 25))
        hits = rank(entries, str(args.get("query", "")), limit)
        body = {"total_tools": len(entries), "matches": [{"name": e.name, "description": e.description[:200]} for e in hits]}
        outcome, tool = "success", "search_tools"
    else:
        e = next((x for x in entries if x.name == str(args.get("name", ""))), None)
        if not e:
            body, outcome = {"error": "unknown tool (or not available to you)"}, "not_found"
        else:
            body, outcome = {"name": e.name, "description": e.description, "inputSchema": e.input_schema}, "success"
        tool = "describe_tool"
    await svc._audit(user, source, "gateway", tool, int((time.monotonic() - t0) * 1000), outcome, None)
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))], isError=outcome != "success")
