import asyncio, os, threading, time
import httpx, pytest, uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

os.environ["MCP_AUTH_TOKEN"] = "tok"
from app import server as S  # noqa: E402


def run(kind, port, tmp):
    os.environ["SERVER"] = kind; S.DATA = tmp; S.TOKEN = "tok"
    srv = uvicorn.Server(uvicorn.Config(S.factory(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started: time.sleep(0.05)
    return srv


async def call(port, name, args=None, list_only=False):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer tok"}) as hc:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.list_tools() if list_only else await s.call_tool(name, args or {})


def test_notes_roundtrip_and_auth(tmp_path):
    srv = run("notes", 8811, tmp_path)
    assert httpx.post("http://127.0.0.1:8811/mcp", json={}).status_code == 401
    assert httpx.get("http://127.0.0.1:8811/healthz").status_code == 200
    asyncio.run(call(8811, "add_note", {"title": "Orders", "body": "3 orders today"}))
    res = asyncio.run(call(8811, "search_notes", {"query": "orders"}))
    assert "3 orders today" in res.content[0].text
    srv.should_exit = True


def test_utils_and_safe_eval(tmp_path):
    srv = run("utils", 8812, tmp_path)
    assert "7" in asyncio.run(call(8812, "calculate", {"expression": "1+2*3"})).content[0].text
    assert asyncio.run(call(8812, "calculate", {"expression": "__import__('os').system('id')"})).isError
    assert asyncio.run(call(8812, "calculate", {"expression": "9**9999"})).isError
    assert not asyncio.run(call(8812, "convert_currency", {"amount": 10, "from_currency": "usd", "to_currency": "inr"})).isError
    srv.should_exit = True


def test_safe_eval_unit():
    assert S.safe_eval("(19.99*3)+4.5") == pytest.approx(64.47)
    for bad in ["a+1", "[1]", "True+1", "2**1000"]:
        with pytest.raises(Exception): S.safe_eval(bad)
