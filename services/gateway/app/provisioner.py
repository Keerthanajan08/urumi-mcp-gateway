"""Store provisioner process:  python -m app.provisioner

Runs as its own Deployment with its own ServiceAccount (the internet-facing gateway has no Kubernetes
permissions for stores). Every LOOP_SECONDS it reconciles all rows of the `stores` table (see app/stores.py).

- Leader election: a Postgres advisory lock, held for the life of one connection. A second replica (or an
  overlapping rollout) waits instead of reconciling the same stores twice.
- Liveness: /healthz turns 503 if the loop has not completed recently, so a wedged process is restarted;
  restarts are safe because all progress is persisted (rows) or idempotent (server-side apply).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import uvicorn
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import load_settings, stores_cfg
from .db import Base, make_engine, make_sessionmaker, startup_lock
from .kube import Kube
from .security import Vault
from .stores import Provisioner

log = logging.getLogger("provisioner")
LOOP_SECONDS = float(os.environ.get("PROVISIONER_LOOP_SECONDS", "5"))
LOCK_KEY = 7_202_604  # arbitrary, constant advisory-lock id for "the store provisioner"
state = {"last_ok": 0.0, "leader": False}


def load_files(directory: str) -> dict[str, str]:
    d = Path(directory)
    return {p.name: p.read_text() for p in sorted(d.iterdir()) if p.is_file() and not p.name.startswith(".")}


async def health(_):
    stale = time.time() - state["last_ok"] > max(60.0, LOOP_SECONDS * 12)
    # a follower that is waiting for the lock is healthy; a leader whose loop stalled is not
    if state["leader"] and stale:
        return JSONResponse({"status": "stalled"}, status_code=503)
    return JSONResponse({"status": "ok", "leader": state["leader"]})


async def run():
    settings = load_settings()
    cfg = stores_cfg(settings)
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    async with startup_lock(engine):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    prov = Provisioner(cfg, sm, Vault(settings.encryption_key), Kube(), load_files(cfg.get("filesDir", "/etc/urumi/store-files")))

    lock_conn = None
    if engine.dialect.name == "postgresql":
        lock_conn = await engine.connect()
        while not (await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})):
            log.info("another provisioner holds the lock; waiting")
            state["last_ok"] = time.time()
            await asyncio.sleep(LOOP_SECONDS)
    state["leader"] = True
    log.info("provisioner is the leader; reconciling every %ss", LOOP_SECONDS)
    while True:
        try:
            await prov.reconcile_all()
            state["last_ok"] = time.time()
        except Exception:  # noqa: BLE001
            log.exception("reconcile loop iteration failed")
        await asyncio.sleep(LOOP_SECONDS)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per API call every loop is noise
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/healthz", health)]), host="0.0.0.0",
                                           port=int(os.environ.get("PORT", "8080")), log_level="warning"))
    await asyncio.gather(server.serve(), run())


if __name__ == "__main__":
    asyncio.run(main())
