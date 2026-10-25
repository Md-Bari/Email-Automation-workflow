from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationFailed
from app.db.models.enums import EmailProvider
from app.integrations.email_providers.base import EmailProviderClient
from app.integrations.email_providers.gmail import GmailProvider
from app.integrations.email_providers.microsoft_graph import MicrosoftGraphProvider
from app.integrations.email_providers.sandbox import SandboxProvider


class ProviderFactory:
    """Builds provider clients. Tests replace this with a factory returning fakes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def is_enabled(self, provider: EmailProvider) -> bool:
        return {
            EmailProvider.GMAIL: self.settings.gmail_enabled,
            EmailProvider.MICROSOFT: self.settings.microsoft_enabled,
            EmailProvider.SANDBOX: self.settings.sandbox_mode,
        }[provider]

    def get(self, provider: EmailProvider, mailbox: str | None = None) -> EmailProviderClient:
        if not self.is_enabled(provider):
            raise ValidationFailed(f"Provider '{provider.value}' is not enabled", code="provider_disabled")
        if provider == EmailProvider.GMAIL:
            return GmailProvider(self.settings)
        if provider == EmailProvider.MICROSOFT:
            return MicrosoftGraphProvider(self.settings)
        return SandboxProvider(mailbox)


_factory: ProviderFactory | None = None


def get_provider_factory() -> ProviderFactory:
    global _factory
    if _factory is None:
        _factory = ProviderFactory()
    return _factory


def set_provider_factory(factory: ProviderFactory | None) -> None:
    global _factory
    _factory = factory
