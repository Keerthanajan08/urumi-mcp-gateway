#!/usr/bin/env python3
"""OAuth on the gateway, exercised by the OFFICIAL MCP Python SDK client (the same flow Claude Code / Cursor run).

    EMAIL=admin@example.com PASSWORD=... python scripts/oauth_check.py [http://app.urumi.localtest.me/mcp]

The SDK does everything itself: 401 + WWW-Authenticate -> protected-resource metadata -> authorization-server
metadata -> dynamic client registration -> PKCE authorize -> token -> MCP calls with the access token.
The only thing this script supplies is the "browser": it opens the authorize page and submits the
dashboard email/password form, then hands the code back to the SDK (what the loopback redirect does
for a desktop client).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
for _s in (sys.stdout, sys.stderr):  # Windows (Git Bash, PowerShell pipes) defaults to cp1252 and cannot print ✔/✘
    _s.reconfigure(encoding="utf-8", errors="replace")

URL = sys.argv[1] if len(sys.argv) > 1 else "http://app.urumi.localtest.me/mcp"
EMAIL, PASSWORD = os.environ["EMAIL"], os.environ["PASSWORD"]
REDIRECT = "http://127.0.0.1:33418/callback"


class Memory(TokenStorage):
    def __init__(self):
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, t): self.tokens = t
    async def get_client_info(self): return self.client
    async def set_client_info(self, c): self.client = c


async def main():
    result: dict = {}

    async def browser(auth_url: str):
        print(f"  browser opens: {auth_url[:90]}…")
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as b:
            page = await b.get(auth_url)
            assert page.status_code == 200 and "Authorize access" in page.text, page.text[:300]
            hidden = dict(re.findall(r'<input type=hidden name="([^"]+)" value="([^"]*)"', page.text))
            hidden = {k: v.replace("&amp;", "&").replace("&quot;", '"') for k, v in hidden.items()}
            r = await b.post(auth_url.split("?")[0], data={**hidden, "email": EMAIL, "password": PASSWORD})
            assert r.status_code == 302, f"authorize POST -> {r.status_code}: {r.text[:200]}"
            loc = r.headers["location"]
            assert loc.startswith(REDIRECT), loc
            q = parse_qs(urlsplit(loc).query)
            result["code"], result["state"] = q["code"][0], q.get("state", [None])[0]
            print("  user signed in; gateway redirected back with an authorization code")

    async def callback():
        return result["code"], result["state"]

    storage = Memory()
    auth = OAuthClientProvider(
        server_url=URL,
        client_metadata=OAuthClientMetadata(client_name="oauth_check.py", redirect_uris=[REDIRECT],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=browser, callback_handler=callback)
    async with httpx.AsyncClient(auth=auth, timeout=60) as hc:
        async with streamable_http_client(URL, http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                print(f"  ✔ connected with OAuth as {EMAIL}: {len(tools)} tools, e.g. {[t.name for t in tools[:3]]}")
                res = await s.call_tool("utils__ping", {})
                print(f"  ✔ utils__ping -> {res.content[0].text!r}")
    print(f"  ✔ client registered as {storage.client.client_id}; access token prefix {storage.tokens.access_token[:4]}…")

    # refresh-token rotation, done by hand against the token endpoint
    base = URL.rsplit("/mcp", 1)[0]
    async with httpx.AsyncClient(timeout=30) as c:
        data = {"grant_type": "refresh_token", "refresh_token": storage.tokens.refresh_token, "client_id": storage.client.client_id}
        r1 = await c.post(f"{base}/oauth/token", data=data)
        r2 = await c.post(f"{base}/oauth/token", data=data)
        print(f"  ✔ refresh -> {r1.status_code}; reusing the same refresh token -> {r2.status_code} {r2.json().get('error')}")
        assert r1.status_code == 200 and r2.status_code == 400
    print("OAUTH CHECK PASSED")


asyncio.run(main())
