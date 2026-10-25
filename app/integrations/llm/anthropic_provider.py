"""Claude provider using the official Anthropic SDK with native structured outputs."""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.llm.base import (
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMRefusalError,
    LLMTransientError,
    T,
)

logger = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        if client is None and not settings.llm_api_key:
            raise LLMError("LLM_API_KEY is required for the anthropic provider", code="llm_not_configured")
        self.settings = settings
        self.model_name = settings.llm_model
        self.client = client or anthropic.AsyncAnthropic(
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        context: dict[str, Any] | None = None,
    ) -> T:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.settings.llm_max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
            "output_config": {"effort": self.settings.llm_effort},
        }
        if self.settings.llm_refusal_fallback:
            # On a safety decline the API re-runs the request on a fallback model automatically.
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            response = await self.client.beta.messages.parse(**kwargs)
        except (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LLMTransientError(f"LLM temporarily unavailable: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 409, 429, 529):
                raise LLMTransientError(f"LLM provider error {exc.status_code}") from exc
            raise LLMError(f"LLM request rejected ({exc.status_code})", code="llm_request_rejected") from exc
        except ValidationError as exc:
            raise LLMOutputError("LLM output failed schema validation") from exc

        logger.info(
            "llm_call",
            model=getattr(response, "model", self.model_name),
            stop_reason=response.stop_reason,
            input_tokens=getattr(response.usage, "input_tokens", None),
            output_tokens=getattr(response.usage, "output_tokens", None),
            request_id=getattr(response, "_request_id", None),
        )
        if response.stop_reason == "refusal":
            raise LLMRefusalError()
        if response.stop_reason == "max_tokens":
            raise LLMOutputError("LLM output was truncated (max_tokens reached)")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMOutputError("LLM returned no structured output")
        if not isinstance(parsed, schema):
            try:
                parsed = schema.model_validate(parsed)
            except ValidationError as exc:
                raise LLMOutputError("LLM output failed schema validation") from exc
        return parsed
