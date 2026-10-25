from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.llm.base import LLMProvider

logger = get_logger(__name__)

_override: LLMProvider | None = None


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider == "anthropic":
        from app.integrations.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if settings.environment == "production":
        logger.warning("mock_llm_provider_in_production")
    from app.integrations.llm.mock_provider import MockLLMProvider

    return MockLLMProvider()


def get_llm_provider() -> LLMProvider:
    return _override or build_llm_provider()


def set_llm_provider(provider: LLMProvider | None) -> None:
    """Test hook: force a specific provider."""
    global _override
    _override = provider
