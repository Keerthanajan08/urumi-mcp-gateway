"""Dashboard REST API (/api/*). Cookie session for browsers; API keys also accepted
(handy for scripts). Admin-only endpoints are guarded by `require_admin`."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ..chat_agent import ChatError, run_chat
from ..mcp_endpoint import authenticate_api_key
from ..config import stores_cfg
from ..models import ApiKey, AuditLog, HostedServer, Server, Store, ToolPolicy, User, now
from .. import stores as store_lib
from .. import hosted
from ..security import hash_password, new_api_key, new_invite_token, verify_password, hash_api_key
from ..service import Principal
from ..ssrf import SSRFError, validate_upstream_url

router = APIRouter(prefix="/api")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")
_login_fails: dict[str, deque] = defaultdict(deque)


# ---------------------------------------------------------------- deps
async def current_user(request: Request) -> Principal:
    st = request.app.state
    uid = st.signer.verify(request.cookies.get("session"))
    if uid:
        async with st.sm() as db:
            u = await db.get(User, uid)
            if u and not u.disabled:
                return Principal(u.id, u.email, u.role, u.name)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        p = await authenticate_api_key(st.sm, auth[7:].strip())
        if p:
            return p
    raise HTTPException(401, "Not authenticated")


async def require_admin(user: Principal = Depends(current_user)) -> Principal:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def _user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "disabled": u.disabled,
            "pending_invite": u.password_hash is None, "created_at": u.created_at}


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    st, email = request.app.state, body.email.strip().lower()
    q = _login_fails[email]
    while q and time.monotonic() - q[0] > 300:
        q.popleft()
    if len(q) >= 10:
        raise HTTPException(429, "Too many failed attempts; try again in a few minutes")
    async with st.sm() as db:
        u = await db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, u.password_hash if u else None)
    if not (u and ok) or u.disabled:
        q.append(time.monotonic())
        raise HTTPException(401, "Invalid email or password")
    response.set_cookie("session", st.signer.issue(u.id), httponly=True, samesite="lax",
                        secure=st.settings.cookie_secure, max_age=st.settings.session_ttl_seconds, path="/")
    return _user_out(u)


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(user: Principal = Depends(current_user)):
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


class AcceptIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)


@router.post("/auth/accept-invite")
async def accept_invite(body: AcceptIn, request: Request):
    async with request.app.state.sm() as db:
        u = await db.scalar(select(User).where(User.invite_token_hash == hash_api_key(body.token)))
        if not u or u.disabled:
            raise HTTPException(400, "Invalid or used invite")
        u.password_hash, u.invite_token_hash = hash_password(body.password), None
        await db.commit()
        return {"ok": True, "email": u.email}


# ---------------------------------------------------------------- users
class InviteIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = ""
    role: str = Field(default="member", pattern="^(admin|member)$")


@router.get("/users")
async def list_users(request: Request, _: Principal = Depends(require_admin)):
    async with request.app.state.sm() as db:
        return [_user_out(u) for u in (await db.scalars(select(User).order_by(User.id))).all()]


@router.post("/users/invite", status_code=201)
async def invite(body: InviteIn, request: Request, _: Principal = Depends(require_admin)):
    token, h = new_invite_token()
    async with request.app.state.sm() as db:
        if await db.scalar(select(User).where(User.email == body.email.lower())):
            raise HTTPException(409, "A user with this email already exists")
        u = User(email=body.email.lower(), name=body.name, role=body.role, invite_token_hash=h)
        db.add(u)
        await db.commit()
        return {"user": _user_out(u), "invite_token": token, "invite_path": f"/accept-invite?token={token}"}


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    disabled: bool | None = None


@router.patch("/users/{uid}")
async def patch_user(uid: int, body: UserPatch, request: Request, admin: Principal = Depends(require_admin)):
    async with request.app.state.sm() as db:
        u = await db.get(User, uid)
        if not u:
            raise HTTPException(404, "No such user")
        if u.id == admin.id and (body.disabled or body.role == "member"):
            raise HTTPException(400, "You cannot demote or disable yourself")
        if body.role is not None:
            u.role = body.role
        if body.disabled is not None:
            u.disabled = body.disabled
            if body.disabled:  # disabling a user kills their keys too
                await db.execute(ApiKey.__table__.update().where(ApiKey.user_id == u.id, ApiKey.revoked_at.is_(None))
                                 .values(revoked_at=now()))
        await db.commit()
        return _user_out(u)


# ---------------------------------------------------------------- api keys
def _key_out(k: ApiKey) -> dict:
    return {"id": k.id, "user_id": k.user_id, "user_email": k.user.email, "name": k.name, "prefix": k.prefix,
            "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at}


class KeyIn(BaseModel):
    name: str = Field(default="", max_length=120)
    user_id: int | None = None  # admins may issue a key for another user


@router.get("/keys")
async def list_keys(request: Request, all: bool = False, user: Principal = Depends(current_user)):
    async with request.app.state.sm() as db:
        q = select(ApiKey).order_by(ApiKey.id.desc())
        if not (all and user.role == "admin"):
            q = q.where(ApiKey.user_id == user.id)
        return [_key_out(k) for k in (await db.scalars(q)).all()]


@router.post("/keys", status_code=201)
async def create_key(body: KeyIn, request: Request, user: Principal = Depends(current_user)):
    target = body.user_id or user.id
    if target != user.id and user.role != "admin":
        raise HTTPException(403, "Only admins can issue keys for other users")
    async with request.app.state.sm() as db:
        owner = await db.get(User, target)
        if not owner or owner.disabled:
            raise HTTPException(404, "No such active user")
        plain, prefix, h = new_api_key()
        k = ApiKey(user_id=target, name=body.name, prefix=prefix, key_hash=h)
        db.add(k)
        await db.commit()
        await db.refresh(k)
        return {**_key_out(k), "key": plain, "note": "Copy this key now; it cannot be shown again."}


@router.delete("/keys/{kid}")
async def revoke_key(kid: int, request: Request, user: Principal = Depends(current_user)):
    async with request.app.state.sm() as db:
        k = await db.get(ApiKey, kid)
        if not k or (k.user_id != user.id and user.role != "admin"):
            raise HTTPException(404, "No such key")
        if k.revoked_at is None:
            k.revoked_at = now()
            await db.commit()
        return _key_out(k)


# ---------------------------------------------------------------- servers
class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = None
    url: str
    auth_type: str = Field(default="none", pattern="^(none|bearer|header)$")
    header_name: str | None = Field(default=None, max_length=100)
    credential: str | None = None
    enabled: bool = True


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = None
    auth_type: str | None = Field(default=None, pattern="^(none|bearer|header)$")
    header_name: str | None = None
    credential: str | None = None  # omitted: keep; "": clear; value: replace
    enabled: bool | None = None


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-")
    return s or "server"


async def _server_out(request: Request, s: Server, admin: bool) -> dict:
    stt = request.app.state.up.state.get(s.slug)
    status = "disabled" if not s.enabled else (stt.status if stt else "unknown")
    tools = [] if not s.enabled or not stt else [
        {"name": t["name"], "exposed_name": f"{s.slug}__{t['name']}", "description": (t.get("description") or "")[:300]}
        for t in stt.tools]
    out = {"id": s.id, "slug": s.slug, "name": s.name, "enabled": s.enabled, "status": status,
           "error": (stt.error if stt and s.enabled else None) if admin else None,
           "tools": tools, "checked_at": stt.checked_at if stt else None,
           "latency_ms": stt.latency_ms if stt else None}
    if admin:  # members never see URLs or any credential detail
        out.update(url=s.url, auth_type=s.auth_type, header_name=s.header_name, has_credential=bool(s.credential_enc))
    return out


@router.get("/servers")
async def list_servers(request: Request, user: Principal = Depends(current_user)):
    async with request.app.state.sm() as db:
        rows = (await db.scalars(select(Server).order_by(Server.id))).all()
    return [await _server_out(request, s, user.role == "admin") for s in rows]


async def _validate(request, url, auth_type, header_name):
    try:
        await validate_upstream_url(url, request.app.state.settings)
    except SSRFError as e:
        raise HTTPException(400, str(e))
    if auth_type == "header" and not header_name:
        raise HTTPException(400, "header_name is required when auth_type is 'header'")


@router.post("/servers", status_code=201)
async def add_server(body: ServerIn, request: Request, _: Principal = Depends(require_admin)):
    st = request.app.state
    await _validate(request, body.url, body.auth_type, body.header_name)
    slug = body.slug or _slugify(body.name)
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug must be 1-24 chars: lowercase letters, digits and hyphens")
    if slug in hosted.RESERVED_SLUGS:
        raise HTTPException(400, f"slug '{slug}' is reserved")
    async with st.sm() as db:
        if await db.scalar(select(Server).where(Server.slug == slug)):
            raise HTTPException(409, f"slug '{slug}' is already taken")
        s = Server(slug=slug, name=body.name, url=body.url, auth_type=body.auth_type, header_name=body.header_name,
                   credential_enc=st.vault.encrypt(body.credential) if body.credential else None, enabled=body.enabled)
        db.add(s)
        await db.commit()
    await st.up.refresh(st.up.to_cfg(s))
    return await _server_out(request, s, True)


@router.patch("/servers/{sid}")
async def edit_server(sid: int, body: ServerPatch, request: Request, _: Principal = Depends(require_admin)):
    st = request.app.state
    async with st.sm() as db:
        s = await db.get(Server, sid)
        if not s:
            raise HTTPException(404, "No such server")
        url, at = body.url or s.url, body.auth_type or s.auth_type
        hn = body.header_name if body.header_name is not None else s.header_name
        if body.url is not None:
            await _validate(request, url, at, hn)
        if body.name is not None:
            s.name = body.name
        s.url, s.auth_type, s.header_name = url, at, hn
        if body.credential is not None:
            s.credential_enc = st.vault.encrypt(body.credential) if body.credential else None
        if body.enabled is not None:
            s.enabled = body.enabled
        await db.commit()
    await st.up.refresh(st.up.to_cfg(s))
    return await _server_out(request, s, True)


@router.delete("/servers/{sid}")
async def remove_server(sid: int, request: Request, _: Principal = Depends(require_admin)):
    st = request.app.state
    async with st.sm() as db:
        s = await db.get(Server, sid)
        if not s:
            raise HTTPException(404, "No such server")
        h = await db.scalar(select(HostedServer).where(HostedServer.server_id == sid))
        if h:
            try:
                await hosted.teardown(_kube(request), s.slug)
            except Exception as e:  # noqa: BLE001 - keep the registration so the admin can retry
                raise HTTPException(502, f"Could not remove the hosted workload: {e}")
            await db.delete(h)
        await db.execute(delete(ToolPolicy).where(ToolPolicy.server_id == sid))
        await db.delete(s)
        await db.commit()
    st.up.state.pop(s.slug, None)
    return {"ok": True}


@router.post("/servers/{sid}/refresh")
async def refresh_server(sid: int, request: Request, _: Principal = Depends(require_admin)):
    st = request.app.state
    async with st.sm() as db:
        s = await db.get(Server, sid)
    if not s:
        raise HTTPException(404, "No such server")
    await st.up.refresh(st.up.to_cfg(s))
    return await _server_out(request, s, True)


class PoliciesIn(BaseModel):
    restricted_tools: dict[str, str]  # upstream tool name -> min_role ("admin")


@router.get("/servers/{sid}/policies")
async def get_policies(sid: int, request: Request, _: Principal = Depends(require_admin)):
    async with request.app.state.sm() as db:
        rows = (await db.scalars(select(ToolPolicy).where(ToolPolicy.server_id == sid))).all()
    return {"restricted_tools": {r.tool_name: r.min_role for r in rows}}


@router.put("/servers/{sid}/policies")
async def set_policies(sid: int, body: PoliciesIn, request: Request, _: Principal = Depends(require_admin)):
    if any(v not in ("admin", "member") for v in body.restricted_tools.values()):
        raise HTTPException(400, "min_role must be 'admin' or 'member'")
    async with request.app.state.sm() as db:
        if not await db.get(Server, sid):
            raise HTTPException(404, "No such server")
        await db.execute(delete(ToolPolicy).where(ToolPolicy.server_id == sid))
        for tool, role in body.restricted_tools.items():
            if role == "admin":  # "member" is the default => no row needed
                db.add(ToolPolicy(server_id=sid, tool_name=tool, min_role=role))
        await db.commit()
    return await get_policies(sid, request, _)


# ---------------------------------------------------------------- tools / audit / chat
@router.get("/tools")
async def my_tools(request: Request, user: Principal = Depends(current_user)):
    return [{"name": e.name, "server": e.slug, "description": e.description, "input_schema": e.input_schema}
            for e in await request.app.state.svc.list_tools(user)]


@router.get("/audit")
async def audit(request: Request, limit: int = 50, offset: int = 0, server: str | None = None,
                outcome: str | None = None, user_email: str | None = None, user: Principal = Depends(current_user)):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    cq = select(func.count()).select_from(AuditLog)
    filters = []
    if user.role != "admin":
        filters.append(AuditLog.user_id == user.id)  # members only ever see their own calls
    elif user_email:
        filters.append(AuditLog.user_email == user_email)
    if server:
        filters.append(AuditLog.server_slug == server)
    if outcome:
        filters.append(AuditLog.outcome == outcome)
    for f in filters:
        q, cq = q.where(f), cq.where(f)
    async with request.app.state.sm() as db:
        rows = (await db.scalars(q.limit(min(limit, 200)).offset(offset))).all()
        total = await db.scalar(cq)
    return {"total": total, "items": [
        {"id": r.id, "ts": r.ts, "user": r.user_email, "source": r.source, "server": r.server_slug, "tool": r.tool,
         "duration_ms": r.duration_ms, "outcome": r.outcome, "error": r.error} for r in rows]}


class ChatIn(BaseModel):
    messages: list[dict]


@router.post("/chat")
async def chat(body: ChatIn, request: Request, user: Principal = Depends(current_user)):
    st = request.app.state
    try:
        return await run_chat(st.settings, st.svc, user, body.messages, client=getattr(st, "llm_client", None))
    except ChatError as e:
        raise HTTPException(e.status, str(e))


# ---------------------------------------------------------------- hosted servers (stand-out: container image -> in-cluster MCP server)
class HostedIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    image: str = Field(min_length=3, max_length=300)
    slug: str | None = None
    port: int = 8000
    path: str = "/mcp"
    env: dict[str, str] = Field(default_factory=dict)  # stored only in a K8s Secret, never echoed back


def _kube(request: Request):
    st = request.app.state
    if st.kube is None:
        st.kube = hosted.KubeClient(st.settings.hosted_namespace)
    return st.kube


@router.post("/hosted", status_code=201)
async def create_hosted(body: HostedIn, request: Request, admin: Principal = Depends(require_admin)):
    st = request.app.state
    cfg = st.settings
    if not cfg.hosted_enabled or not cfg.hosted_namespace:
        raise HTTPException(400, "Hosted servers are disabled (chart value hosted.enabled=false).")
    slug = body.slug or _slugify(body.name)
    if not SLUG_RE.match(slug) or slug in hosted.RESERVED_SLUGS:
        raise HTTPException(400, "invalid or reserved slug")
    try:
        hosted.validate_request(body.image, body.port, body.path, body.env, cfg.hosted_allowed_prefixes)
    except hosted.HostedError as e:
        raise HTTPException(400, str(e))
    async with st.sm() as db:
        if await db.scalar(select(Server).where(Server.slug == slug)):
            raise HTTPException(409, f"slug '{slug}' is already taken")
        if (await db.scalar(select(func.count()).select_from(HostedServer))) >= cfg.hosted_max_servers:
            raise HTTPException(409, f"limit of {cfg.hosted_max_servers} hosted servers reached")
        s = Server(slug=slug, name=body.name, auth_type="none", enabled=True,
                   url=hosted.internal_url(slug, cfg.hosted_namespace, body.port, body.path))
        db.add(s)
        await db.flush()
        db.add(HostedServer(server_id=s.id, image=body.image, port=body.port, created_by=admin.email))
        manifests = hosted.build_manifests(ns=cfg.hosted_namespace, slug=slug, image=body.image, port=body.port,
                                           env=body.env, run_as_user=cfg.hosted_run_as_user)
        try:  # apply is idempotent (server-side apply), so a failed request can simply be retried
            await hosted.deploy(_kube(request), manifests)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            try:
                await hosted.teardown(_kube(request), slug)  # best-effort cleanup of half-created objects
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(502, f"Deployment failed: {e}")
        await db.commit()
    from ..upstream import UpstreamState
    st.up.state.setdefault(slug, UpstreamState()).starting_until = __import__("time").time() + 180
    await st.up.refresh(st.up.to_cfg(s))
    return await _server_out(request, s, True)


@router.get("/hosted")
async def list_hosted(request: Request, _: Principal = Depends(require_admin)):
    async with request.app.state.sm() as db:
        rows = (await db.execute(select(HostedServer, Server).join(Server, Server.id == HostedServer.server_id))).all()
    return [{"server_id": s.id, "slug": s.slug, "name": s.name, "image": h.image, "port": h.port,
             "created_by": h.created_by, "created_at": h.created_at} for h, s in rows]


# ---------------------------------------------------------------- observability summary for the dashboard
@router.get("/stats")
async def stats(request: Request, hours: int = 24, _: Principal = Depends(require_admin)):
    since = now() - __import__("datetime").timedelta(hours=max(1, min(hours, 168)))
    async with request.app.state.sm() as db:
        rows = (await db.execute(select(AuditLog.server_slug, AuditLog.outcome, AuditLog.duration_ms)
                                 .where(AuditLog.ts >= since).limit(20000))).all()
    per: dict[str, dict] = {}
    for slug, outcome, ms in rows:
        d = per.setdefault(slug or "-", {"calls": 0, "errors": 0, "ms": [], "outcomes": {}})
        d["calls"] += 1
        d["errors"] += outcome != "success"
        d["ms"].append(ms)
        d["outcomes"][outcome] = d["outcomes"].get(outcome, 0) + 1
    out = []
    for slug, d in sorted(per.items()):
        ms = sorted(d["ms"])
        pct = lambda p: ms[min(len(ms) - 1, int(len(ms) * p))]  # noqa: E731
        stt = request.app.state.up.state.get(slug)
        out.append({"server": slug, "calls": d["calls"], "error_rate": round(d["errors"] / d["calls"], 3),
                    "p50_ms": pct(0.5), "p95_ms": pct(0.95), "outcomes": d["outcomes"],
                    "status": stt.status if stt else None, "unreachable_reason": stt.error if stt else None})
    return {"hours": hours, "servers": out, "total_calls": len(rows)}



# ---------------------------------------------------------------- stores (bonus: multi-store provisioning)
# The gateway only records desired state; the store provisioner (separate Deployment, its own ServiceAccount)
# turns it into Kubernetes objects. So this internet-facing process never holds cluster-wide permissions.
class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str | None = None


def _store_out(request: Request, s: Store, user: Principal) -> dict:
    cfg = stores_cfg(request.app.state.settings)
    return {"id": s.id, "slug": s.slug, "name": s.name, "status": s.status, "reason": s.reason,
            "url": store_lib.url_for(cfg, s.slug), "owner": s.owner_email, "created_at": s.created_at,
            "ready_at": s.ready_at, "namespace": store_lib.namespace_for(cfg, s.slug), "tool_prefix": f"{s.slug}__",
            "can_manage": user.role == "admin" or s.owner_id == user.id}


def _stores_enabled(request: Request) -> dict:
    cfg = stores_cfg(request.app.state.settings)
    if not cfg.get("enabled"):
        raise HTTPException(400, "Store provisioning is disabled (chart value stores.enabled=false).")
    return cfg


@router.get("/stores")
async def list_stores(request: Request, user: Principal = Depends(current_user)):
    cfg = stores_cfg(request.app.state.settings)
    async with request.app.state.sm() as db:
        rows = (await db.scalars(select(Store).order_by(Store.id))).all()
    return {"enabled": bool(cfg.get("enabled")), "max_per_user": cfg.get("maxPerUser"),
            "timeout_seconds": cfg.get("provisionTimeoutSeconds"), "stores": [_store_out(request, s, user) for s in rows]}


@router.post("/stores", status_code=201)
async def create_store(body: StoreIn, request: Request, user: Principal = Depends(current_user)):
    cfg = _stores_enabled(request)
    st = request.app.state
    slug = body.slug or _slugify(body.name)[:20].strip("-")
    try:
        store_lib.validate_slug(slug, cfg)
    except store_lib.StoreError as e:
        raise HTTPException(400, str(e))
    if slug in hosted.RESERVED_SLUGS:
        raise HTTPException(400, f"'{slug}' is reserved.")
    async with st.sm() as db:
        if await db.scalar(select(Store).where(Store.slug == slug)) or await db.scalar(select(Server).where(Server.slug == slug)):
            raise HTTPException(409, f"'{slug}' is already used by another store or server; pick another short name.")
        mine = await db.scalar(select(func.count()).select_from(Store).where(Store.owner_id == user.id, Store.status != "deleting"))
        if mine >= int(cfg.get("maxPerUser", 2)):
            raise HTTPException(409, f"You already have {mine} stores (limit {cfg.get('maxPerUser')} per user). Delete one first.")
        s = Store(slug=slug, name=body.name, owner_id=user.id, owner_email=user.email, status="provisioning",
                  reason="queued", deadline=int(time.time() + int(cfg.get("provisionTimeoutSeconds", 900))),
                  secrets_enc=st.vault.encrypt(__import__("json").dumps(store_lib.new_secrets())))
        db.add(s)
        await db.commit()
        return _store_out(request, s, user)


async def _owned_store(request: Request, sid: int, user: Principal, db) -> Store:
    s = await db.get(Store, sid)
    if not s:
        raise HTTPException(404, "No such store")
    if not (user.role == "admin" or s.owner_id == user.id):
        raise HTTPException(403, "Only the store's owner or an admin can do that")
    return s


@router.delete("/stores/{sid}")
async def delete_store(sid: int, request: Request, user: Principal = Depends(current_user)):
    _stores_enabled(request)
    st = request.app.state
    from sqlalchemy.orm.exc import StaleDataError
    for _ in range(3):  # the provisioner may be updating the same row; retry on an optimistic-lock conflict
        try:
            async with st.sm() as db:
                s = await _owned_store(request, sid, user, db)
                s.status, s.reason = "deleting", "queued for deletion"
                srv = await db.scalar(select(Server).where(Server.slug == s.slug))
                if srv is not None and srv.url == store_lib.mcp_url(stores_cfg(st.settings), s.slug):
                    srv.enabled = False   # tools disappear right now; the provisioner removes the registration
                await db.commit()
                return _store_out(request, s, user)
        except StaleDataError:
            continue
    raise HTTPException(409, "The store is being updated; try again.")


@router.post("/stores/{sid}/retry")
async def retry_store(sid: int, request: Request, user: Principal = Depends(current_user)):
    cfg = _stores_enabled(request)
    async with request.app.state.sm() as db:
        s = await _owned_store(request, sid, user, db)
        if s.status != "failed":
            raise HTTPException(400, "Only a failed store can be retried")
        s.status, s.reason = "provisioning", "retry queued"
        s.deadline = int(time.time() + int(cfg.get("provisionTimeoutSeconds", 900)))
        await db.commit()
        return _store_out(request, s, user)
