"""Async engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def build_engine(url: str | None = None, *, null_pool: bool = False) -> AsyncEngine:
    settings = get_settings()
    url = url or settings.database_url
    kwargs: dict = {"echo": settings.database_echo, "pool_pre_ping": True}
    if null_pool:
        # Celery tasks each run their own event loop; pooled connections must not cross loops.
        kwargs["poolclass"] = NullPool
    elif not url.startswith("sqlite"):
        kwargs["pool_size"] = settings.database_pool_size
    return create_async_engine(url, **kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@lru_cache
def get_engine() -> AsyncEngine:
    return build_engine()


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(get_engine())


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Session for Celery tasks (fresh NullPool engine bound to the current event loop)."""
    engine = build_engine(null_pool=True)
    try:
        async with build_sessionmaker(engine)() as session:
            yield session
    finally:
        await engine.dispose()
