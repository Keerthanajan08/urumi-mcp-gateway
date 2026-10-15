#!/usr/bin/env python3
"""Scripted run of the assignment's "definition of done" against a LIVE deployment.

    pip install httpx "mcp==1.30.0"
    ADMIN_PW=$(kubectl -n urumi get secret urumi-secrets -o jsonpath='{.data.bootstrap-admin-password}' | base64 -d) \
      python scripts/dod_check.py

It talks only to the public URLs (Ingress), exactly like a person would:
  store   : a shopper puts a product in the cart and checks out with Cash on delivery (WooCommerce Store API,
            the same endpoints the storefront's cart/checkout pages call)
  user A  : admin; uses the dashboard chat agent (needs an LLM key configured in the chart)
  user B  : member invited by A; uses the /mcp endpoint with only URL + API key (an MCP client, like Inspector)
  control : revoke B's key, disable a server, kill an upstream pod, read the audit log
It does not replace the demo video (a real MCP client such as Claude Code is still the thing to show), but it
proves every step works before you record. Set KILL_POD=0 to skip the step that scales notes-mcp to zero.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
for _s in (sys.stdout, sys.stderr):  # Windows (Git Bash, PowerShell pipes) defaults to cp1252 and cannot print ✔/✘
    _s.reconfigure(encoding="utf-8", errors="replace")

DOMAIN = os.environ.get("DOMAIN", "urumi.localtest.me")
SCHEME = os.environ.get("SCHEME", "http")
APP, STORE = f"{SCHEME}://app.{DOMAIN}", f"{SCHEME}://store.{DOMAIN}"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PW = os.environ["ADMIN_PW"]
NS = os.environ.get("NS", "urumi")
KILL_POD = os.environ.get("KILL_POD", "1") == "1"
OK, FAIL = "\033[32m✔\033[0m", "\033[31m✘\033[0m"
failures: list[str] = []


def check(cond: bool, what: str, detail: str = ""):
    print(f"  {OK if cond else FAIL} {what}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(what)


def section(t: str):
    print(f"\n\033[1m{t}\033[0m")


# ------------------------------------------------------------------ storefront (shopper)
def place_order(product_id: int, who: str) -> int:
    with httpx.Client(base_url=f"{STORE}/wp-json/wc/store/v1", timeout=30) as c:
        # Cookie session + nonce, exactly like the storefront in a browser. (WooCommerce 10.3's header-based
        # Cart-Token session path throws a fatal error, so it is deliberately not used here.)
        r = c.get("/cart")
        h = {"Nonce": r.headers["Nonce"]}
        c.post("/cart/add-item", json={"id": product_id, "quantity": 1}, headers=h).raise_for_status()
        addr = {"first_name": who, "last_name": "Shopper", "address_1": "1 Demo St", "city": "Bengaluru",
                "state": "KA", "postcode": "560001", "country": "IN", "email": f"{who.lower()}@example.com", "phone": "9999999999"}
        r = c.post("/checkout", headers=h, json={"billing_address": addr, "shipping_address": addr, "payment_method": "cod"})
        r.raise_for_status()
        return r.json()["order_id"]


def store_products() -> list[dict]:
    return httpx.get(f"{STORE}/wp-json/wc/store/v1/products", params={"per_page": 100}, timeout=30).json()


# ------------------------------------------------------------------ dashboard API (cookie session, like the React app)
def login(email: str, pw: str) -> httpx.Client:
    c = httpx.Client(base_url=APP, timeout=90)
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    r.raise_for_status()
    return c


def chat(c: httpx.Client, text: str) -> dict:
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": text}]})
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ external MCP client (URL + key only)
async def mcp_session(key: str, fn):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as hc:
        async with streamable_http_client(f"{APP}/mcp", http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


def mcp(key: str, fn):
    return asyncio.run(mcp_session(key, fn))


def text(res) -> str:
    """All text blocks of a tool result (FastMCP returns one block per item of a list result)."""
    return "\n".join(c.text for c in res.content if getattr(c, "text", None))


async def _list(s):
    return [t.name for t in (await s.list_tools()).tools]


def kubectl(*a: str):
    subprocess.run(["kubectl", "-n", NS, *a], check=True, capture_output=True)


def server_status(c: httpx.Client, slug: str) -> dict:
    return next(s for s in c.get("/api/servers").json() if s["slug"] == slug)


def main():
    run = secrets.token_hex(2)
    section("Store")
    seed = next((p for p in store_products() if p.get("is_purchasable")), None)
    if seed is None:
        a0 = login(ADMIN_EMAIL, ADMIN_PW)
        chat(a0, "Create a product called Starter Tee for 15")
        seed = next(p for p in store_products() if p["name"] == "Starter Tee")
    oid0 = place_order(seed["id"], "Asha")
    check(oid0 > 0, f"shopper checked out '{seed['name']}' with Cash on delivery -> order #{oid0}")

    section("Chat agent, as user A (admin)")
    A = login(ADMIN_EMAIL, ADMIN_PW)
    name = f"Blue Mug {run}"
    res = chat(A, f"Create a product called {name} for 12.50")
    tools_used = [t["tool"] for t in res["tool_calls"]]
    check("woocommerce__create_product" in tools_used or any("create_product" in json.dumps(t["arguments"]) for t in res["tool_calls"]),
          "agent called woocommerce__create_product", str(res)[:300])
    prod = next((p for p in store_products() if p["name"] == name), None)
    check(prod is not None, f"'{name}' appears on the storefront")
    if prod:
        page = httpx.get(prod["permalink"], timeout=30, follow_redirects=True)
        check(page.status_code == 200 and name in page.text, "product page renders on the storefront", prod["permalink"])
        oid = place_order(prod["id"], "Ravi")
        check(oid > 0, f"order #{oid} placed by hand for '{name}'")
        res = chat(A, "What orders came in today?")
        check(any(t["tool"].endswith("list_orders") or "list_orders" in json.dumps(t["arguments"]) for t in res["tool_calls"]),
              "agent called woocommerce__list_orders")
        check(f'"id": {oid}' in res["reply"] or f'"id":{oid}' in res["reply"] or str(oid) in res["reply"],
              f"agent answer includes order #{oid}", res["reply"][:300])

    section("External MCP client, as user B (member)")
    email_b = f"member-{run}@example.com"
    inv = A.post("/api/users/invite", json={"email": email_b, "name": "Member B", "role": "member"}).json()
    pw_b = secrets.token_urlsafe(12)
    httpx.post(f"{APP}/api/auth/accept-invite", json={"token": inv["invite_token"], "password": pw_b}).raise_for_status()
    B = login(email_b, pw_b)
    key = B.post("/api/keys", json={"name": "claude-code"}).json()
    key_b = key["key"]
    names = mcp(key_b, _list)
    servers = {n.split("__")[0] for n in names}
    enabled = {s["slug"] for s in A.get("/api/servers").json() if s["status"] == "connected"}
    check(servers == enabled or names == ["gateway__search_tools", "gateway__describe_tool", "gateway__call_tool"],
          f"B sees the tools of every enabled server: {len(names)} tools from {sorted(servers)}", f"dashboard says {sorted(enabled)}")
    check("notes__ping" in names and "utils__ping" in names, "same-named tools are namespaced (notes__ping vs utils__ping)")

    async def orders_and_note(s):
        o = await s.call_tool("woocommerce__list_orders", {"today_only": True})
        n = await s.call_tool("notes__add_note", {"title": f"Orders today ({run})", "body": text(o)[:1000]})
        return o, n
    o, n = mcp(key_b, orders_and_note)
    check(not o.isError and f'"id": {oid0}' in text(o), f"B asked the orders question via MCP and got today's orders (incl. #{oid0})")
    check(not n.isError, "one task across two upstream servers (woocommerce + notes) succeeded")
    check(all("token" not in json.dumps(s) and "credential" not in s for s in B.get("/api/servers").json()),
          "B's view of servers has no URL/credential fields")

    section("Team and control")
    A.delete(f"/api/keys/{key['id']}").raise_for_status()
    try:
        mcp(key_b, _list)
        rejected = False
    except BaseException as e:  # noqa: BLE001 - the SDK surfaces the 401 inside an exception group
        rejected = "401" in repr(e) or "Unauthorized" in repr(e) or "ExceptionGroup" in type(e).__name__
    check(rejected, "revoked key is rejected on B's next MCP session")
    r = httpx.post(f"{APP}/mcp", headers={"Authorization": f"Bearer {key_b}"}, json={})
    check(r.status_code == 401, f"raw request with the revoked key -> HTTP {r.status_code}")

    key2 = B.post("/api/keys", json={"name": "second"}).json()["key"]
    utils = server_status(A, "utils")
    A.patch(f"/api/servers/{utils['id']}", json={"enabled": False}).raise_for_status()
    names = mcp(key2, _list)
    check(not any(n.startswith("utils__") for n in names), "disabled server's tools vanish from the external client")
    chat_tools = [t["name"] for t in A.get("/api/tools").json()]
    check(not any(n.startswith("utils__") for n in chat_tools), "...and from the chat agent's tool list")
    res = chat(A, "ping everything")
    check(not any(t["tool"].startswith("utils__") and not t["is_error"] for t in res["tool_calls"]), "...and the chat agent cannot call them")
    A.patch(f"/api/servers/{utils['id']}", json={"enabled": True}).raise_for_status()
    check(server_status(A, "utils")["status"] == "connected", "re-enabled server is connected again")

    if KILL_POD:
        kubectl("scale", "deploy/notes-mcp", "--replicas=0")
        kubectl("wait", "--for=delete", "pod", "-l", "app.kubernetes.io/component=notes-mcp", "--timeout=60s")
        st = {}
        for _ in range(30):
            st = server_status(A, "notes")
            if st["status"] == "unreachable":
                break
            time.sleep(1)
        check(st["status"] == "unreachable", f"dashboard shows notes as Unreachable ({st.get('error')})")

        async def mixed(s):
            dead = await s.call_tool("notes__list_notes", {})
            alive = await s.call_tool("woocommerce__list_products", {})
            return dead, alive, [t.name for t in (await s.list_tools()).tools]
        dead, alive, names = mcp(key2, mixed)
        check(dead.isError and "unreachable" in dead.content[0].text,
              f"call to the dead server returns a clear error: {dead.content[0].text[:90]!r}")
        check(not alive.isError, "other servers keep working")
        check(not any(n.startswith("notes__") for n in names), "dead server's tools are withheld from tools/list")
        kubectl("scale", "deploy/notes-mcp", "--replicas=1")
        kubectl("rollout", "status", "deploy/notes-mcp", "--timeout=120s")

    audit = A.get("/api/audit", params={"limit": 200}).json()["items"]
    by_user = {(i["user"], i["source"], i["server"], i["tool"]) for i in audit}
    check((ADMIN_EMAIL, "chat", "woocommerce", "create_product") in by_user, "audit: A's chat create_product attributed to A (source chat)")
    check((email_b, "mcp", "woocommerce", "list_orders") in by_user, "audit: B's MCP list_orders attributed to B (source mcp)")
    check((email_b, "mcp", "notes", "add_note") in by_user, "audit: B's notes add_note attributed to B")
    if KILL_POD:
        check(any(i["user"] == email_b and i["server"] == "notes" and i["outcome"] in ("unreachable", "not_found") for i in audit),
              "audit: the failed call to the dead server is logged with its outcome")
    mine = B.get("/api/audit").json()["items"]
    check(mine and all(i["user"] == email_b for i in mine), "members only see their own audit rows")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
