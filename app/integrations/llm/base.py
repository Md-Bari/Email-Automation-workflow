"""LLM provider abstraction. The LLM produces data only; it never authorizes or triggers actions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.exceptions import IntegrationError, TransientError

T = TypeVar("T", bound=BaseModel)


class LLMError(IntegrationError):
    """Non-retryable LLM failure (bad configuration, authentication, invalid request)."""

    def __init__(self, message: str, code: str = "llm_error") -> None:
        super().__init__(message, code=code, retryable=False)


class LLMTransientError(TransientError):
    """Rate limit, timeout, overload or outage; safe to retry with backoff."""

    def __init__(self, message: str, code: str = "llm_unavailable") -> None:
        super().__init__(message, code=code)


class LLMOutputError(TransientError):
    """The model returned output that failed schema validation (retried a bounded number of times)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="llm_invalid_output")


class LLMRefusalError(LLMError):
    def __init__(self, message: str = "The model declined to process this content") -> None:
        super().__init__(message, code="llm_refusal")


class LLMProvider(ABC):
    model_name: str

    @abstractmethod
    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        context: dict[str, Any] | None = None,
    ) -> T:
        """Return a validated instance of ``schema``.

        ``context`` carries the raw facts for providers that do not read prompts (the mock).
        """
