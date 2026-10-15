from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kw = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw = {}
    return create_async_engine(url, **kw)


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


STARTUP_LOCK = 7_202_603


@asynccontextmanager
async def startup_lock(engine):
    """Serialize schema creation + bootstrap across processes. Several gateway replicas (and the store
    provisioner) start at once against a fresh database; without this, two of them insert the bootstrap admin
    concurrently and one crashes on the unique constraint. Postgres only; SQLite runs a single process."""
    if engine.dialect.name != "postgresql":
        yield
        return
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": STARTUP_LOCK})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": STARTUP_LOCK})
