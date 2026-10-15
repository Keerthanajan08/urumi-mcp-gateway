"""OAuth 2.1 for the MCP endpoint (MCP authorization spec): browser login instead of pasting a key.

Flow a client such as Claude Code / Cursor performs automatically:
  1. POST /mcp without a token -> 401 + `WWW-Authenticate: Bearer resource_metadata="<.../.well-known/oauth-protected-resource>"`
  2. GET  /.well-known/oauth-protected-resource   (RFC 9728)  -> names this server as its own authorization server
  3. GET  /.well-known/oauth-authorization-server (RFC 8414)  -> endpoints, S256 only
  4. POST /oauth/register                          (RFC 7591)  -> public client, exact redirect URIs
  5. browser: GET/POST /oauth/authorize            -> the user signs in with their dashboard email+password
  6. POST /oauth/token  (authorization_code + PKCE S256, or refresh_token) -> opaque `umo_` access / `umr_` refresh tokens

Design notes
- Opaque random tokens stored only as SHA-256 (like API keys); access 1 h, refresh 30 d and rotated on use.
- Public clients only (PKCE is mandatory); redirect URIs must match exactly; codes are single use, 5 min.
- The authorize page asks for email+password every time (no cookie reuse), so it is not CSRF-able and a
  stolen dashboard cookie is not enough to mint tokens. Failed logins are throttled.
- Tokens carry the user's own role, so tool policies / audit / rate limits apply exactly as for API keys.
- Disabling a user kills their OAuth tokens on the next request. The token is bound to this gateway only and is
  never forwarded upstream (see threat model).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from .models import OAuthClient, OAuthCode, OAuthToken, User
from .security import verify_password
from .service import Principal

router = APIRouter()
ACCESS_TTL, REFRESH_TTL, CODE_TTL = 3600, 30 * 86400, 300
_fails: dict[str, deque] = defaultdict(deque)
_h = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731


def base_url(request: Request) -> str:
    return request.app.state.settings.public_url or str(request.base_url).rstrip("/")


def resource_metadata_url(request: Request) -> str:
    return f"{base_url(request)}/.well-known/oauth-protected-resource"


def _err(error: str, desc: str, status=400):
    return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def redirect_uri_ok(uri: str) -> bool:
    u = urlsplit(uri)
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":
        return u.hostname in ("localhost", "127.0.0.1", "::1")  # native-app loopback redirect
    return u.scheme not in ("javascript", "data", "file", "vbscript", "about", "blob", "ftp") and bool(u.netloc or u.path)  # app schemes e.g. cursor://


# ---------------------------------------------------------------- discovery
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request):
    b = base_url(request)
    return {"resource": f"{b}/mcp", "authorization_servers": [b], "bearer_methods_supported": ["header"]}


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request):
    b = base_url(request)
    return {"issuer": b, "authorization_endpoint": f"{b}/oauth/authorize", "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]}


# ---------------------------------------------------------------- dynamic client registration
@router.post("/oauth/register", status_code=201)
async def register(request: Request):
    try:
        body = await request.json()
        uris = body["redirect_uris"]
        assert isinstance(uris, list) and 1 <= len(uris) <= 5 and all(isinstance(u, str) and redirect_uri_ok(u) for u in uris)
    except Exception:  # noqa: BLE001
        return _err("invalid_redirect_uri", "redirect_uris must be 1-5 https, loopback-http or app-scheme URIs")
    name = str(body.get("client_name") or "MCP client")[:120]
    cid = "umc_" + secrets.token_urlsafe(16)
    async with request.app.state.sm() as db:
        if len((await db.scalars(select(OAuthClient.id))).all()) > 500:  # cheap abuse bound on open registration
            return _err("temporarily_unavailable", "too many registered clients", 503)
        db.add(OAuthClient(client_id=cid, name=name, redirect_uris=json.dumps(uris)))
        await db.commit()
    return {"client_id": cid, "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}


# ---------------------------------------------------------------- authorize (browser)
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Authorize MCP client</title><style>body{{font:16px system-ui;max-width:26rem;margin:10vh auto;padding:0 1rem}}
input,button{{width:100%;padding:.6rem;margin:.3rem 0;font:inherit;box-sizing:border-box}}.e{{color:#b00020}}</style>
<h1>Authorize access</h1><p><b>{client}</b> wants to use your team's MCP tools as you.</p>{error}
<form method=post action="/oauth/authorize">{hidden}<input name=email type=email placeholder=Email required autofocus>
<input name=password type=password placeholder=Password required><button>Sign in and allow</button></form>
<p><small>Only continue if you started this from your MCP client. Redirects to: {redirect}</small></p>"""


async def _load_client(request: Request, client_id: str, redirect_uri: str):
    async with request.app.state.sm() as db:
        c = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))
    if not c or redirect_uri not in json.loads(c.redirect_uris):
        return None  # never redirect to an unregistered URI
    return c


def _page(c, params: dict, error: str = "", status=200):
    hidden = "".join(f'<input type=hidden name="{k}" value="{html.escape(v, quote=True)}">' for k, v in params.items())
    body = PAGE.format(client=html.escape(c.name), hidden=hidden, redirect=html.escape(params["redirect_uri"]),
                       error=f'<p class=e>{html.escape(error)}</p>' if error else "")
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                           "Content-Security-Policy": "frame-ancestors 'none'"})


def _check_params(response_type, challenge, method):
    if response_type != "code":
        return "unsupported_response_type"
    if not challenge or method != "S256":
        return "PKCE with S256 is required"
    return None


@router.get("/oauth/authorize")
async def authorize_get(request: Request, response_type: str = "", client_id: str = "", redirect_uri: str = "",
                        code_challenge: str = "", code_challenge_method: str = "", state: str = "", resource: str = ""):
    c = await _load_client(request, client_id, redirect_uri)
    if not c:
        return HTMLResponse("Unknown client or redirect URI.", status_code=400)
    if (e := _check_params(response_type, code_challenge, code_challenge_method)):
        return HTMLResponse(html.escape(e), status_code=400)
    # RFC 8707: a client may only ask for tokens for THIS gateway's MCP endpoint (tokens are audience-bound to it).
    if resource and resource.rstrip("/") != f"{base_url(request)}/mcp":
        return HTMLResponse("invalid_target: this server only issues tokens for its own /mcp resource.", status_code=400)
    return _page(c, {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge,
                     "code_challenge_method": code_challenge_method, "state": state, "response_type": response_type})


@router.post("/oauth/authorize")
async def authorize_post(request: Request, email: str = Form(...), password: str = Form(...), client_id: str = Form(""),
                         redirect_uri: str = Form(""), code_challenge: str = Form(""),
                         code_challenge_method: str = Form(""), state: str = Form(""), response_type: str = Form("")):
    c = await _load_client(request, client_id, redirect_uri)
    if not c or _check_params(response_type, code_challenge, code_challenge_method):
        return HTMLResponse("Invalid authorization request.", status_code=400)
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge,
              "code_challenge_method": code_challenge_method, "state": state, "response_type": response_type}
    key, now_ = f"{email.lower()}|{request.client.host if request.client else ''}", time.time()
    q = _fails[key]
    while q and now_ - q[0] > 300:
        q.popleft()
    if len(q) >= 8:
        return _page(c, params, "Too many attempts. Try again in a few minutes.", 429)
    async with request.app.state.sm() as db:
        u = await db.scalar(select(User).where(User.email == email.lower()))
        if not u or u.disabled or not verify_password(password, u.password_hash):
            q.append(now_)
            return _page(c, params, "Invalid email or password.", 401)
        code = "umcode_" + secrets.token_urlsafe(32)
        db.add(OAuthCode(code_hash=_h(code), client_id=client_id, user_id=u.id, redirect_uri=redirect_uri,
                         challenge=code_challenge, expires_at=int(now_) + CODE_TTL))
        await db.commit()
    q.clear()
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'code': code, **({'state': state} if state else {})})}", status_code=302,
                            headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- token
async def _issue(db, client_id: str, user_id: int) -> dict:
    access, refresh = "umo_" + secrets.token_urlsafe(32), "umr_" + secrets.token_urlsafe(32)
    t = int(time.time())
    db.add(OAuthToken(access_hash=_h(access), refresh_hash=_h(refresh), client_id=client_id, user_id=user_id,
                      expires_at=t + ACCESS_TTL, refresh_expires_at=t + REFRESH_TTL))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh}


@router.post("/oauth/token")
async def token(request: Request, grant_type: str = Form(""), code: str = Form(""), redirect_uri: str = Form(""),
                client_id: str = Form(""), code_verifier: str = Form(""), refresh_token: str = Form("")):
    t = int(time.time())
    async with request.app.state.sm() as db:
        if grant_type == "authorization_code":
            row = await db.get(OAuthCode, _h(code)) if code else None
            if row:
                await db.delete(row)  # single use, even if the checks below fail
                await db.commit()
            if not row or row.expires_at < t or row.client_id != client_id or row.redirect_uri != redirect_uri:
                return _err("invalid_grant", "invalid, expired or mismatched code")
            digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
            if not code_verifier or not secrets.compare_digest(digest, row.challenge):
                return _err("invalid_grant", "PKCE verification failed")
            user = await db.get(User, row.user_id)
            if not user or user.disabled:
                return _err("invalid_grant", "user is disabled")
            out = await _issue(db, client_id, user.id)
        elif grant_type == "refresh_token":
            old = await db.scalar(select(OAuthToken).where(OAuthToken.refresh_hash == _h(refresh_token))) if refresh_token else None
            if not old or old.revoked_at or old.refresh_expires_at < t or old.client_id != client_id or old.user.disabled:
                return _err("invalid_grant", "invalid or expired refresh token")
            old.revoked_at = t  # rotation: a refresh token works once
            out = await _issue(db, client_id, old.user_id)
        else:
            return _err("unsupported_grant_type", "use authorization_code or refresh_token")
        await db.commit()
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- used by the MCP endpoint
async def authenticate_oauth_token(sm, tok: str) -> Principal | None:
    if not tok.startswith("umo_"):
        return None
    async with sm() as db:
        row = await db.scalar(select(OAuthToken).where(OAuthToken.access_hash == _h(tok)))
        if not row or row.revoked_at or row.expires_at < time.time() or row.user.disabled:
            return None
        u = row.user
        return Principal(u.id, u.email, u.role, u.name)
