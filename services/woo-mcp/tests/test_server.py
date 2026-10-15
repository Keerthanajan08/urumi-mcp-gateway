import asyncio, json, threading, time
import httpx, pytest, uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings
from app.server import create_app
from app.wc_client import WooClient

ORDERS = [{"id": 7, "status": "processing", "total": "25.00", "currency": "USD",
           "date_created": "2026-10-08T10:00:00", "billing": {"first_name": "Asha", "last_name": "K"},
           "line_items": [{"name": "Mug", "quantity": 1}], "payment_method_title": "COD",
           "shipping_total": "0.00"}]
seen = []


def wc_handler(request: httpx.Request) -> httpx.Response:
    seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
    p = request.url.path
    if p.endswith("/system_status"):
        return httpx.Response(200, json={"environment": {}})
    if p.endswith("/products") and request.method == "POST":
        b = json.loads(request.content)
        return httpx.Response(201, json={"id": 99, **b, "price": b["regular_price"], "permalink": "http://x/p/99"})
    if p.endswith("/products"):
        return httpx.Response(200, json=[{"id": 1, "name": "Mug", "price": "10"}])
    if p.endswith("/orders/7") and request.method == "PUT":
        return httpx.Response(200, json={**ORDERS[0], "status": json.loads(request.content)["status"]})
    if p.endswith("/orders/7"):
        return httpx.Response(200, json=ORDERS[0])
    if p.endswith("/orders/404"):
        return httpx.Response(404, json={"message": "Invalid ID."})
    if p.endswith("/orders"):
        return httpx.Response(200, json=ORDERS)
    return httpx.Response(500)


@pytest.fixture(scope="module")
def server():
    s = Settings("http://wc", "ck_test", "cs_test", "s3cret", "Asia/Kolkata", 5, 8765, "basic", "https")
    client = WooClient(s.wc_base_url, "ck_test", "cs_test", transport=httpx.MockTransport(wc_handler))
    cfg = uvicorn.Config(create_app(s, client), host="127.0.0.1", port=8765, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(50):
        if srv.started: break
        time.sleep(0.1)
    yield "http://127.0.0.1:8765"
    srv.should_exit = True


async def with_session(url, fn, token="s3cret"):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as hc:
        async with streamable_http_client(f"{url}/mcp", http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


def test_health_open_and_mcp_requires_token(server):
    assert httpx.get(f"{server}/healthz").status_code == 200
    assert httpx.get(f"{server}/readyz").status_code == 200
    assert httpx.post(f"{server}/mcp", json={}).status_code == 401
    assert httpx.post(f"{server}/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lists_five_tools(server):
    async def f(s): return {t.name for t in (await s.list_tools()).tools}
    names = asyncio.run(with_session(server, f))
    assert names == {"list_products", "create_product", "list_orders", "get_order", "update_order_status"}


def test_create_product_and_orders_today(server):
    async def f(s):
        c = await s.call_tool("create_product", {"name": "Tee", "regular_price": "19.99"})
        o = await s.call_tool("list_orders", {"today_only": True})
        return c, o
    c, o = asyncio.run(with_session(server, f))
    assert not c.isError and "Tee" in c.content[0].text
    assert not o.isError and "Asha" in o.content[0].text
    params = [p for (m, path, p, a) in seen if path.endswith("/orders") and "after" in p][-1]
    # local midnight converted to UTC, flagged as GMT (WooCommerce would otherwise read it in the site timezone)
    assert params["dates_are_gmt"] == "true"
    from datetime import datetime, time as dtime, timezone
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Kolkata")  # the fixture's store timezone
    want = datetime.combine(datetime.now(tz).date(), dtime.min, tzinfo=tz).astimezone(timezone.utc)
    assert params["after"] == want.strftime("%Y-%m-%dT%H:%M:%S")
    assert seen and all(a and a.startswith("Basic ") for (_m, _p, _q, a) in seen)


def test_errors_are_clean_and_validated(server):
    async def f(s):
        bad = await s.call_tool("get_order", {"order_id": 404})
        badprice = await s.call_tool("create_product", {"name": "x", "regular_price": "abc"})
        badstatus = await s.call_tool("update_order_status", {"order_id": 7, "status": "bogus"})
        ok = await s.call_tool("update_order_status", {"order_id": 7, "status": "completed"})
        return bad, badprice, badstatus, ok
    bad, badprice, badstatus, ok = asyncio.run(with_session(server, f))
    assert bad.isError and "404" in bad.content[0].text
    assert badprice.isError and "decimal" in badprice.content[0].text
    assert badstatus.isError
    assert not ok.isError and "completed" in ok.content[0].text
