#!/usr/bin/env python3
"""Bonus acceptance test (multi-store provisioning) against a LIVE deployment with stores.enabled=true.

    ADMIN_PW=... python scripts/stores_check.py [store-a-slug] [store-b-slug]

1. waits until both stores are Ready (creating them if they do not exist)
2. adds a product to each through the gateway (<slug>__create_product) and places a COD order on each storefront
3. asks the chat agent to compare today's orders across the stores
4. checks isolation: store A's MCP pod cannot reach store B's WordPress / MariaDB (NetworkPolicies)
5. deletes store B: its tools disappear from tools/list at once, then its namespace, volumes, secrets and
   registration are gone
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
for _s in (sys.stdout, sys.stderr):  # Windows (Git Bash, PowerShell pipes) defaults to cp1252 and cannot print ✔/✘
    _s.reconfigure(encoding="utf-8", errors="replace")

APP = os.environ.get("APP", "http://app.urumi.localtest.me")
ADMIN_EMAIL, ADMIN_PW = os.environ.get("ADMIN_EMAIL", "admin@example.com"), os.environ["ADMIN_PW"]
A_SLUG, B_SLUG = (sys.argv[1:3] + ["chai-corner", "book-nook"][len(sys.argv[1:3]):])[:2]
OK, FAIL = "\033[32m✔\033[0m", "\033[31m✘\033[0m"
failures: list[str] = []


def check(cond, what, detail=""):
    print(f"  {OK if cond else FAIL} {what}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(what)


def kubectl(*a, check_=True) -> str:
    return subprocess.run(["kubectl", *a], capture_output=True, text=True, check=check_).stdout


async def _mcp(key, fn):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as hc:
        async with streamable_http_client(f"{APP}/mcp", http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


def mcp(key, fn):
    return asyncio.run(_mcp(key, fn))


def place_order(store_url: str, product_id: int, who: str) -> int:
    with httpx.Client(base_url=f"{store_url}/wp-json/wc/store/v1", timeout=30) as c:
        h = {"Nonce": c.get("/cart").headers["Nonce"]}
        c.post("/cart/add-item", json={"id": product_id, "quantity": 2}, headers=h).raise_for_status()
        addr = {"first_name": who, "last_name": "Buyer", "address_1": "1 Market St", "city": "San Francisco", "state": "CA",
                "postcode": "94103", "country": "US", "email": f"{who.lower()}@example.com", "phone": "5555555555"}
        r = c.post("/checkout", headers=h, json={"billing_address": addr, "shipping_address": addr, "payment_method": "cod"})
        r.raise_for_status()
        return r.json()["order_id"]


def main():
    A = httpx.Client(base_url=APP, timeout=120)
    A.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW}).raise_for_status()
    key = A.post("/api/keys", json={"name": "stores-check"}).json()["key"]

    print("\n\033[1m1) Two stores, each in its own namespace\033[0m")
    have = {s["slug"]: s for s in A.get("/api/stores").json()["stores"]}
    for slug in (A_SLUG, B_SLUG):
        if slug not in have:
            A.post("/api/stores", json={"name": slug.replace("-", " ").title(), "slug": slug}).raise_for_status()
    t0 = time.time()
    while True:
        st = {s["slug"]: s for s in A.get("/api/stores").json()["stores"]}
        if all(st[s]["status"] in ("ready", "failed") for s in (A_SLUG, B_SLUG)) or time.time() - t0 > 900:
            break
        time.sleep(5)
    for s in (A_SLUG, B_SLUG):
        check(st[s]["status"] == "ready", f"store '{s}' is Ready in namespace {st[s]['namespace']} at {st[s]['url']}", str(st[s].get("reason")))

    print("\n\033[1m2) A product and an order in each store\033[0m")
    names = mcp(key, lambda s: s.list_tools())
    names = [t.name for t in names.tools]
    check(f"{A_SLUG}__list_orders" in names and f"{B_SLUG}__list_orders" in names,
          f"both stores' tools are in the gateway ({A_SLUG}__*, {B_SLUG}__*) without manual registration")
    orders = {}
    for slug, price, who in ((A_SLUG, "4.50", "Asha"), (B_SLUG, "12.00", "Bilal")):
        res = mcp(key, lambda s, slug=slug, price=price: s.call_tool(f"{slug}__create_product", {"name": f"{slug} special", "regular_price": price}))
        pid = json.loads(res.content[0].text)["id"]
        orders[slug] = place_order(st[slug]["url"], pid, who)
        check(orders[slug] > 0, f"{slug}: product #{pid} created via {slug}__create_product; storefront order #{orders[slug]}")

    print("\n\033[1m3) Chat agent compares today's orders across the two stores\033[0m")
    res = A.post("/api/chat", json={"messages": [{"role": "user", "content": "Compare today's orders across my stores"}]}).json()
    used = [t["tool"] for t in res["tool_calls"]]
    check(f"{A_SLUG}__list_orders" in used and f"{B_SLUG}__list_orders" in used, f"agent called {used}")
    # Model-agnostic: the data each store returned must contain its new order, and the answer must cover each store
    # (a real model summarizes; it need not repeat every order id).
    results = {t["tool"].split("__")[0]: t["result_preview"] for t in res["tool_calls"] if t["tool"].endswith("__list_orders")}
    check(all(f'"id":{orders[s]}' in results.get(s, "").replace(" ", "") for s in orders),
          "each store's tool result contains that store's new order")
    reply = res["reply"].lower()
    check(all(s.replace("-", " ") in reply or s in reply for s in orders), "the answer covers both stores",
          res["reply"][:300])
    print("     agent answer:", res["reply"][:400].replace("\n", " "))

    print("\n\033[1m4) Isolation between stores (NetworkPolicies)\033[0m")
    probe = ("import socket,sys\ns=socket.socket();s.settimeout(3)\ntry:\n s.connect((sys.argv[1],int(sys.argv[2])));print('OPEN')\n"
             "except Exception as e:\n print('BLOCKED',type(e).__name__)")
    for host, port in ((f"wordpress.store-{B_SLUG}.svc.cluster.local", 80), (f"mariadb.store-{B_SLUG}.svc.cluster.local", 3306),
                       ("mariadb.urumi.svc.cluster.local", 3306), ("postgres.urumi.svc.cluster.local", 5432)):
        out = kubectl("-n", f"store-{A_SLUG}", "exec", "deploy/woo-mcp", "--", "python", "-c", probe, host, str(port), check_=False).strip()
        check(out.startswith("BLOCKED"), f"{A_SLUG}'s MCP server -> {host}:{port}: {out}")
    out = kubectl("-n", f"store-{A_SLUG}", "exec", "deploy/woo-mcp", "--", "python", "-c", probe, f"wordpress.store-{A_SLUG}.svc.cluster.local", "80", check_=False).strip()
    check(out == "OPEN", f"...but it can reach its own WordPress: {out}")

    print(f"\n\033[1m5) Delete '{B_SLUG}': tools and resources are gone\033[0m")
    sid = st[B_SLUG]["id"]
    A.delete(f"/api/stores/{sid}").raise_for_status()
    names = [t.name for t in mcp(key, lambda s: s.list_tools()).tools]
    check(not any(n.startswith(f"{B_SLUG}__") for n in names), "its tools vanished from tools/list immediately")
    check(any(n.startswith(f"{A_SLUG}__") for n in names), f"{A_SLUG}'s tools are unaffected")
    pvs_before = kubectl("get", "pv", "-o", "jsonpath={range .items[*]}{.spec.claimRef.namespace}{' '}{end}").split()
    check(f"store-{B_SLUG}" in pvs_before, f"(before) PersistentVolumes bound in store-{B_SLUG}: {pvs_before.count('store-' + B_SLUG)}")
    t0 = time.time()
    while time.time() - t0 < 300:
        if B_SLUG not in {s["slug"] for s in A.get("/api/stores").json()["stores"]}:
            break
        time.sleep(3)
    check(B_SLUG not in {s["slug"] for s in A.get("/api/stores").json()["stores"]}, f"store row removed after {int(time.time() - t0)}s")
    check(subprocess.run(["kubectl", "get", "ns", f"store-{B_SLUG}"], capture_output=True).returncode != 0, f"namespace store-{B_SLUG} is gone")
    time.sleep(10)
    pvs = kubectl("get", "pv", "-o", "jsonpath={range .items[*]}{.spec.claimRef.namespace}{' '}{end}").split()
    check(f"store-{B_SLUG}" not in pvs, "its PersistentVolumes are gone (reclaimPolicy Delete)")
    check(B_SLUG not in {s["slug"] for s in A.get("/api/servers").json()}, "its MCP registration is gone")
    r = mcp(key, lambda s: s.call_tool(f"{B_SLUG}__list_orders", {}))
    check(r.isError, f"calling one of its old tools now fails cleanly: {r.content[0].text[:80]!r}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
