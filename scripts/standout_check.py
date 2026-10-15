#!/usr/bin/env python3
"""Live checks for stand-out items that are exercised through the public URL:
tool permissions, many-tools search mode, per-user rate limit headers of behaviour, and observability.

    ADMIN_PW=... python scripts/standout_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
for _s in (sys.stdout, sys.stderr):  # Windows (Git Bash, PowerShell pipes) defaults to cp1252 and cannot print ✔/✘
    _s.reconfigure(encoding="utf-8", errors="replace")

APP = os.environ.get("APP", "http://app.urumi.localtest.me")
ADMIN_EMAIL, ADMIN_PW = os.environ.get("ADMIN_EMAIL", "admin@example.com"), os.environ["ADMIN_PW"]
OK, FAIL = "\033[32m✔\033[0m", "\033[31m✘\033[0m"
failures: list[str] = []


def check(cond, what, detail=""):
    print(f"  {OK if cond else FAIL} {what}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(what)


def login(email, pw):
    c = httpx.Client(base_url=APP, timeout=60)
    c.post("/api/auth/login", json={"email": email, "password": pw}).raise_for_status()
    return c


async def _with(key, fn, mode=None):
    h = {"Authorization": f"Bearer {key}"}
    if mode:
        h["X-Gateway-Tools"] = mode
    async with httpx.AsyncClient(headers=h, timeout=60) as hc:
        async with streamable_http_client(f"{APP}/mcp", http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


def run(key, fn, mode=None):
    return asyncio.run(_with(key, fn, mode))


async def names(s):
    return [t.name for t in (await s.list_tools()).tools]


def main():
    A = login(ADMIN_EMAIL, ADMIN_PW)
    run_id = secrets.token_hex(2)
    email = f"perm-{run_id}@example.com"
    inv = A.post("/api/users/invite", json={"email": email, "role": "member"}).json()
    pw = secrets.token_urlsafe(12)
    httpx.post(f"{APP}/api/auth/accept-invite", json={"token": inv["invite_token"], "password": pw}).raise_for_status()
    M = login(email, pw)
    mkey = M.post("/api/keys", json={"name": "perm"}).json()["key"]
    akey = A.post("/api/keys", json={"name": f"admin-{run_id}"}).json()["key"]
    woo = next(s for s in A.get("/api/servers").json() if s["slug"] == "woocommerce")

    print("\n\033[1mTool permissions\033[0m")
    A.put(f"/api/servers/{woo['id']}/policies",
          json={"restricted_tools": {"create_product": "admin", "update_order_status": "admin"}}).raise_for_status()
    mn, an = run(mkey, names), run(akey, names)
    check("woocommerce__create_product" not in mn and "woocommerce__update_order_status" not in mn,
          "restricted tools are hidden from the member's tools/list")
    check("woocommerce__list_orders" in mn, "member still sees read tools (list_orders)")
    check("woocommerce__create_product" in an, "admin still sees create_product")
    res = run(mkey, lambda s: s.call_tool("woocommerce__create_product", {"name": "Nope", "regular_price": "1"}))
    check(res.isError and "role" in res.content[0].text, f"member calling it directly is rejected: {res.content[0].text!r}")
    chat_tools = [t["name"] for t in M.get("/api/tools").json()]
    check("woocommerce__create_product" not in chat_tools, "the member's chat agent does not get the restricted tool either")
    audit = A.get("/api/audit", params={"user_email": email, "outcome": "denied"}).json()["items"]
    check(any(i["tool"] == "create_product" for i in audit), "the rejected attempt is in the audit log as 'denied'")
    A.put(f"/api/servers/{woo['id']}/policies", json={"restricted_tools": {}}).raise_for_status()

    print("\n\033[1mMany tools: search -> describe -> call\033[0m")
    meta = run(mkey, names, mode="search")
    check(meta == ["gateway__search_tools", "gateway__describe_tool", "gateway__call_tool"],
          f"search mode advertises only 3 meta-tools (got {meta})")

    async def flow(s):
        found = json.loads((await s.call_tool("gateway__search_tools", {"query": "orders today"})).content[0].text)
        top = found["matches"][0]["name"]
        desc = json.loads((await s.call_tool("gateway__describe_tool", {"name": top})).content[0].text)
        out = await s.call_tool("gateway__call_tool", {"name": top, "arguments": {"today_only": True}})
        return found, top, desc, out
    found, top, desc, out = run(mkey, flow, mode="search")
    check(found["total_tools"] >= 14, f"search sees all {found['total_tools']} visible tools")
    check(top.endswith("__list_orders"), f"'orders today' ranks a list_orders tool first (got {top}; every store has one)")
    check("today_only" in json.dumps(desc.get("inputSchema", {})), "describe_tool returns the real input schema")
    check(not out.isError, "call_tool executes it")
    a = A.get("/api/audit", params={"user_email": email, "server": top.split("__")[0]}).json()["items"]
    check(any(i["tool"] == "list_orders" for i in a), f"audit records the real server/tool ({top}), not the meta-tool")

    A.put(f"/api/servers/{woo['id']}/policies", json={"restricted_tools": {"create_product": "admin"}}).raise_for_status()
    hits = json.loads(run(mkey, lambda s: s.call_tool("gateway__search_tools", {"query": "create product"}), mode="search").content[0].text)
    check(all(h["name"] != "woocommerce__create_product" for h in hits["matches"]), "search never reveals a tool the member may not use")
    res = run(mkey, lambda s: s.call_tool("gateway__call_tool", {"name": "woocommerce__create_product",
                                                                  "arguments": {"name": "x", "regular_price": "1"}}), mode="search")
    check(res.isError, "...and call_tool cannot be used to bypass the policy")
    A.put(f"/api/servers/{woo['id']}/policies", json={"restricted_tools": {}}).raise_for_status()

    print("\n\033[1mObservability\033[0m")
    st = A.get("/api/stats").json()
    check(st["total_calls"] > 0 and all({"error_rate", "p50_ms", "p95_ms"} <= set(s) for s in st["servers"]),
          f"/api/stats: per-server calls, error rate, p50/p95 ({len(st['servers'])} servers)")
    check(M.get("/api/stats").status_code == 403, "stats are admin-only")
    r = httpx.get(f"{APP}/metrics")
    check("gateway_tool_calls_total" not in r.text, "/metrics is NOT reachable through the Ingress")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
