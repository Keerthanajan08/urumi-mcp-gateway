"""End-to-end tests for the stand-out features over real HTTP: OAuth (PKCE, refresh rotation),
many-tools search mode (permissions + audit), and the rate limit shared across two gateway replicas."""
import asyncio, base64, hashlib, secrets, tempfile
from urllib.parse import parse_qs, urlsplit

import httpx, pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.main import create_app
from .test_gateway_flow import Thread, call, free_port, login, make_upstream, mcp_session, tool_names

CB = "http://127.0.0.1:9/callback"


def settings(db, **kw):
    base = dict(database_url=f"sqlite+aiosqlite:///{db}/gw.db", encryption_key=Fernet.generate_key().decode(),
                session_secret="y" * 32, bootstrap_admin_email="admin@t.io", bootstrap_admin_password="adminpass1",
                ssrf_allow_private=True, health_interval=1, rate_limit_per_minute=0, public_url="")
    base.update(kw)
    return Settings(**base)


@pytest.fixture(scope="module")
def stack():
    db, p_up, p_gw = tempfile.mkdtemp(), free_port(), free_port()
    up = Thread(make_upstream("docs"), p_up)
    s = settings(db, tools_mode="full")
    s.public_url = f"http://127.0.0.1:{p_gw}"
    gw = Thread(create_app(s), p_gw)
    base = f"http://127.0.0.1:{p_gw}"
    admin = login(base, "admin@t.io", "adminpass1")
    assert admin.post("/api/servers", json={"name": "Docs", "url": f"http://127.0.0.1:{p_up}/mcp"}).status_code == 201
    yield {"base": base, "admin": admin, "db": db, "p_up": p_up}
    gw.stop(); up.stop()


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def get_code(base, cid, challenge, password="adminpass1", state="xyz"):
    return httpx.post(f"{base}/oauth/authorize", follow_redirects=False, data={
        "email": "admin@t.io", "password": password, "client_id": cid, "redirect_uri": CB, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def test_oauth_discovery_and_challenge(stack):
    b = stack["base"]
    r = httpx.post(f"{b}/mcp", json={})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    prm = httpx.get(f"{b}/.well-known/oauth-protected-resource").json()
    assert prm["authorization_servers"] == [b] and prm["resource"] == f"{b}/mcp"
    meta = httpx.get(f"{b}/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["registration_endpoint"].endswith("/oauth/register")


def test_oauth_full_flow_refresh_rotation_and_mcp_access(stack):
    b = stack["base"]
    assert httpx.post(f"{b}/oauth/register", json={"redirect_uris": ["https://evil.example/cb", "javascript:alert(1)"]}).status_code == 400
    cid = httpx.post(f"{b}/oauth/register", json={"client_name": "Test", "redirect_uris": [CB]}).json()["client_id"]
    # unregistered redirect URI is never honoured
    assert httpx.get(f"{b}/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": "http://127.0.0.1:1/x",
                                                       "code_challenge": "c", "code_challenge_method": "S256"}).status_code == 400
    # RFC 8707: tokens can only be requested for this gateway's own /mcp resource
    assert httpx.get(f"{b}/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CB,
                                                       "code_challenge": "c", "code_challenge_method": "S256",
                                                       "resource": "https://other.example/mcp"}).status_code == 400
    assert httpx.get(f"{b}/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CB,
                                                       "code_challenge": "c", "code_challenge_method": "S256",
                                                       "resource": f"{b}/mcp"}).status_code == 200
    # PKCE is mandatory
    assert httpx.get(f"{b}/oauth/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CB}).status_code == 400
    verifier, challenge = pkce()
    assert get_code(b, cid, challenge, password="wrong").status_code == 401
    r = get_code(b, cid, challenge)
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]
    tok = lambda **d: httpx.post(f"{b}/oauth/token", data=d)  # noqa: E731
    assert tok(grant_type="authorization_code", code=code, redirect_uri=CB, client_id=cid, code_verifier="not-the-verifier").status_code == 400
    assert tok(grant_type="authorization_code", code=code, redirect_uri=CB, client_id=cid, code_verifier=verifier).status_code == 400  # code was burnt
    v2, c2 = pkce()
    code2 = parse_qs(urlsplit(get_code(b, cid, c2).headers["location"]).query)["code"][0]
    t = tok(grant_type="authorization_code", code=code2, redirect_uri=CB, client_id=cid, code_verifier=v2).json()
    assert t["access_token"].startswith("umo_") and t["refresh_token"].startswith("umr_")
    # the OAuth token works on /mcp exactly like an API key
    assert any(n.endswith("docs__search") for n in tool_names(b, t["access_token"]))
    # refresh rotates: the old refresh token dies
    t2 = tok(grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=cid).json()
    assert t2["access_token"] != t["access_token"]
    assert tok(grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=cid).status_code == 400
    assert httpx.post(f"{b}/mcp", json={}, headers={"Authorization": "Bearer umo_bogus"}).status_code == 401


def test_tool_search_mode_permissions_and_audit(stack):
    b, admin = stack["base"], stack["admin"]
    key = admin.post("/api/keys", json={"name": "k"}).json()["key"]

    async def names(s, mode):
        return sorted(t.name for t in (await s.list_tools()).tools)
    # per-connection override via header: search mode shows only the 3 meta tools
    async def go():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}", "X-Gateway-Tools": "search"}) as hc:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            async with streamable_http_client(f"{b}/mcp", http_client=hc) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    listed = sorted(t.name for t in (await s.list_tools()).tools)
                    found = await s.call_tool("gateway__search_tools", {"query": "search docs"})
                    run = await s.call_tool("gateway__call_tool", {"name": "docs__search", "arguments": {"query": "orders"}})
                    nested = await s.call_tool("gateway__call_tool", {"name": "gateway__call_tool", "arguments": {}})
                    return listed, found, run, nested
    listed, found, run, nested = asyncio.run(go())
    assert listed == ["gateway__call_tool", "gateway__describe_tool", "gateway__search_tools"]
    assert "docs__search" in found.content[0].text and not run.isError and "docs result for orders" in run.content[0].text
    assert nested.isError
    items = admin.get("/api/audit?limit=20").json()["items"]
    assert any(i["server"] == "docs" and i["tool"] == "search" for i in items), "real call must be audited under real server/tool"
    assert any(i["server"] == "gateway" and i["tool"] == "search_tools" for i in items)


def test_rate_limit_is_shared_across_replicas(tmp_path):
    p_up = free_port(); up = Thread(make_upstream("docs"), p_up)
    s1, s2 = (settings(str(tmp_path), rate_limit_per_minute=3) for _ in range(2))
    s2.encryption_key = s1.encryption_key
    a, b_ = free_port(), free_port()
    g1 = Thread(create_app(s1), a); g2 = Thread(create_app(s2), b_)  # two "replicas", one database
    try:
        admin = login(f"http://127.0.0.1:{a}", "admin@t.io", "adminpass1")
        admin.post("/api/servers", json={"name": "Docs", "url": f"http://127.0.0.1:{p_up}/mcp"})
        key = admin.post("/api/keys", json={"name": "k"}).json()["key"]
        results = [call(f"http://127.0.0.1:{p}", key, "docs__search", {"query": "x"}) for p in (a, b_, a, b_, a)]
        assert [bool(r.isError) for r in results] == [False, False, False, True, True]
        assert "Rate limit" in results[3].content[0].text
    finally:
        g1.stop(); g2.stop(); up.stop()
