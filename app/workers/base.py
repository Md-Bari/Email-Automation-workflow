from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.core.config import get_settings
from app.core.logging import correlation_id_var
from app.workers.jobs import RetryableJobError

T = TypeVar("T")

RETRY_OPTIONS: dict[str, Any] = {
    "autoretry_for": (RetryableJobError,),
    "retry_backoff": 5,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": get_settings().task_max_retries,
}


def run_async(coro: Coroutine[Any, Any, T], correlation_id: str | None = None) -> T:
    """Run a coroutine in a fresh event loop (one per task; DB engines use NullPool accordingly)."""
    token = correlation_id_var.set(correlation_id)
    try:
        return asyncio.run(coro)
    finally:
        correlation_id_var.reset(token)
