"""Fixed-window rate limiting backed by the key-value store."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request

from app.core.exceptions import RateLimitedError
from app.core.kvstore import KeyValueStore, get_kv_store


def client_ip(request: Request) -> str:
    # Behind a trusted proxy run uvicorn with --proxy-headers so request.client is the real address.
    return request.client.host if request.client else "unknown"


async def hit(store: KeyValueStore, key: str, limit: int, window_seconds: int) -> None:
    count, remaining = await store.incr(f"ratelimit:{key}", window_seconds)
    if count > limit:
        raise RateLimitedError(retry_after=remaining or window_seconds)


def rate_limit(scope: str, limit: int, window_seconds: int) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency limiting requests per client IP for a named scope."""

    async def dependency(request: Request, store: KeyValueStore = Depends(get_kv_store)) -> None:
        await hit(store, f"{scope}:{client_ip(request)}", limit, window_seconds)

    return dependency
