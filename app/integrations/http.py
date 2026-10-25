"""HTTP helper with bounded exponential backoff for transient provider errors."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            from datetime import UTC, datetime

            return max((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return None


async def request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int,
    is_retryable: Callable[[httpx.Response], bool] | None = None,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> httpx.Response:
    """Perform a request, retrying on 408/429/5xx (or ``is_retryable``) and transport errors.

    Returns the final response (which may still be an error) or raises the last transport error.
    """
    attempt = 0
    while True:
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt >= max_retries:
                raise
            delay = min(max_delay, base_delay * 2**attempt) * (0.5 + random.random() / 2)  # noqa: S311
            logger.warning("http_transport_retry", url=url.split("?")[0], attempt=attempt, error=type(exc).__name__)
        else:
            retryable = response.status_code in RETRYABLE_STATUS or (is_retryable and is_retryable(response))
            if not retryable or attempt >= max_retries:
                return response
            delay = _retry_after_seconds(response) or (
                min(max_delay, base_delay * 2**attempt) * (0.5 + random.random() / 2)  # noqa: S311
            )
            delay = min(delay, max_delay)
            logger.warning("http_status_retry", url=url.split("?")[0], status=response.status_code, attempt=attempt)
        attempt += 1
        await asyncio.sleep(delay)
