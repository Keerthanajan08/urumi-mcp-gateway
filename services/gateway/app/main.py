from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from starlette.routing import Route

from .api.routes import router
from .oauth import router as oauth_router
from .config import Settings, load_settings
from .db import Base, make_engine, make_sessionmaker, startup_lock
from .mcp_endpoint import build_mcp_app
from .models import Server, User
from .security import SessionSigner, Vault, hash_password
from .service import GatewayService
from .upstream import UpstreamManager

log = logging.getLogger("gateway")


async def bootstrap(settings: Settings, sm, vault: Vault):
    """Idempotent first-run setup: admin account + the first (WooCommerce) upstream."""
    async with sm() as db:
        if settings.bootstrap_admin_email and settings.bootstrap_admin_password:
            email = settings.bootstrap_admin_email.lower()
            if not await db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name="Admin", role="admin",
                            password_hash=hash_password(settings.bootstrap_admin_password)))
        for item in json.loads(settings.seed_servers or "[]"):
            if await db.scalar(select(Server).where(Server.slug == item["slug"])):
                continue
            token = item.get("token") or ""
            db.add(Server(slug=item["slug"], name=item["name"], url=item["url"],
                          auth_type="bearer" if token else "none",
                          credential_enc=vault.encrypt(token) if token else None))
        await db.commit()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    vault, signer = Vault(settings.encryption_key), SessionSigner(settings.session_secret, settings.session_ttl_seconds)
    up = UpstreamManager(settings, vault, sm)
    svc = GatewayService(settings, up, sm)
    manager, mcp_asgi = build_mcp_app(svc, sm)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with startup_lock(engine):  # replicas starting together must not race on schema/bootstrap
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await bootstrap(settings, sm, vault)
        up.start()
        async with manager.run():
            yield
        await up.stop()
        await engine.dispose()

    app = FastAPI(title="Urumi MCP Gateway", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings, app.state.sm, app.state.vault, app.state.signer = settings, sm, vault, signer
    app.state.up, app.state.svc = up, svc
    app.include_router(router)
    app.include_router(oauth_router)
    app.state.kube = None  # created lazily by the hosted-server endpoints
    # Plain ASGI route (not a Mount) so POST /mcp works without a 307 redirect to /mcp/.
    app.router.routes.append(Route("/mcp", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"]))

    @app.get("/healthz")
    async def healthz():  # liveness: the process is serving
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():  # readiness: database reachable
        try:
            async with sm() as db:
                await db.execute(text("SELECT 1"))
            if not up.first_check_done:  # bounded: each upstream probe has its own timeout
                return JSONResponse({"status": "not-ready", "reason": "first upstream check pending"}, status_code=503)
            return {"status": "ready"}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"status": "not-ready", "reason": type(e).__name__}, status_code=503)

    @app.get("/metrics")  # not routed by the Ingress; scraped in-cluster only
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    host = os.environ.get("HOSTNAME", "gateway").encode()

    class ServedBy:  # pure ASGI (no response buffering): lets the scaling demo show requests spread over replicas
        def __init__(self, inner): self.inner = inner

        async def __call__(self, scope, receive, send):
            async def _send(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", []).append((b"x-served-by", host))
                await send(message)
            await self.inner(scope, receive, _send if scope["type"] == "http" else send)

    app.add_middleware(ServedBy)
    return app


def factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()
