"""Small key-value abstraction over Redis (OAuth state, rate limits, locks).

The in-memory implementation is used by tests and as a single-process development fallback.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


class KeyValueStore(ABC):
    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def pop(self, key: str) -> str | None:
        """Atomically read and delete (single-use values such as OAuth state)."""

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        """Increment a counter, setting a TTL on first use. Returns (count, seconds_remaining)."""

    @abstractmethod
    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool: ...

    @abstractmethod
    async def ping(self) -> bool: ...


class RedisStore(KeyValueStore):
    def __init__(self, client: Redis) -> None:
        self.client = client

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self.client.set(key, value, ex=ttl_seconds)

    async def get(self, key: str) -> str | None:
        value = await self.client.get(key)
        return value.decode() if isinstance(value, bytes) else value

    async def pop(self, key: str) -> str | None:
        value = await self.client.getdel(key)
        return value.decode() if isinstance(value, bytes) else value

    async def delete(self, key: str) -> None:
        await self.client.delete(key)

    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
        return int(count), max(int(ttl), 0)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self.client.set(key, "1", ex=ttl_seconds, nx=True))

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False


class MemoryStore(KeyValueStore):
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _alive(self, key: str) -> tuple[str, float] | None:
        item = self._data.get(key)
        if item and item[1] < time.monotonic():
            self._data.pop(key, None)
            return None
        return item

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl_seconds)

    async def get(self, key: str) -> str | None:
        item = self._alive(key)
        return item[0] if item else None

    async def pop(self, key: str) -> str | None:
        async with self._lock:
            item = self._alive(key)
            self._data.pop(key, None)
            return item[0] if item else None

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        async with self._lock:
            item = self._alive(key)
            if item is None:
                expires = time.monotonic() + ttl_seconds
                count = 1
            else:
                count, expires = int(item[0]) + 1, item[1]
            self._data[key] = (str(count), expires)
            return count, max(int(expires - time.monotonic()), 0)

    async def acquire_lock(self, key: str, ttl_seconds: int) -> bool:
        async with self._lock:
            if self._alive(key):
                return False
            self._data[key] = ("1", time.monotonic() + ttl_seconds)
            return True

    async def ping(self) -> bool:
        return True


@lru_cache
def get_redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, socket_timeout=5, socket_connect_timeout=5)


@lru_cache
def get_kv_store() -> KeyValueStore:
    return RedisStore(get_redis_client())
