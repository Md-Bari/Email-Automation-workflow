"""Provider-neutral mailbox interface.

Providers are stateless with respect to credentials: every call receives an access token.
Token refresh and persistence are handled by ``AccountService.with_token``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.exceptions import IntegrationError, TransientError


class ProviderAuthError(IntegrationError):
    """Access token rejected (401). The caller should refresh and retry once."""

    def __init__(self, message: str = "Access token rejected") -> None:
        super().__init__(message, code="provider_auth", retryable=False)


class CredentialsRevokedError(IntegrationError):
    """Refresh token revoked/expired; the user must reconnect the account."""

    def __init__(self, message: str = "Credentials revoked; reconnect the account") -> None:
        super().__init__(message, code="credentials_revoked", retryable=False)


class InsufficientScopeError(IntegrationError):
    def __init__(self, message: str = "The account did not grant the required permission") -> None:
        super().__init__(message, code="insufficient_scope", retryable=False)


class CursorExpiredError(IntegrationError):
    """The incremental-sync cursor is too old; fall back to a bounded resync."""

    def __init__(self) -> None:
        super().__init__("Synchronization cursor expired", code="cursor_expired", retryable=False)


class MessageNotFoundError(IntegrationError):
    def __init__(self, message_id: str) -> None:
        super().__init__(f"Message {message_id} no longer exists", code="message_not_found", retryable=False)


class ProviderUnavailableError(TransientError):
    def __init__(self, message: str = "Email provider temporarily unavailable") -> None:
        super().__init__(message, code="provider_unavailable")


class ProviderRequestError(IntegrationError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message, code="provider_error", retryable=False)
        self.status_code = status_code


@dataclass(slots=True)
class Address:
    email: str
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"email": self.email, "name": self.name}


@dataclass(slots=True)
class ProviderMessage:
    provider_message_id: str
    thread_id: str | None
    sender: Address
    to: list[Address]
    cc: list[Address]
    reply_to: str | None
    subject: str
    body_text: str
    received_at: datetime
    internet_message_id: str | None = None
    references: str | None = None
    labels: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    snippet: str | None = None
    is_automated: bool = False


@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


@dataclass(slots=True)
class MailboxProfile:
    email_address: str
    # Starting cursor for incremental sync (Gmail historyId); None if the provider has none.
    cursor: str | None


@dataclass(slots=True)
class ChangeSet:
    added_ids: list[str]
    deleted_ids: list[str]
    new_cursor: str | None


@dataclass(slots=True)
class OutgoingReply:
    to: str
    subject: str
    body: str
    from_address: str
    thread_id: str | None
    in_reply_to: str | None
    references: str | None
    original_provider_message_id: str


class EmailProviderClient(ABC):
    name: str
    send_scope: str | None = None
    read_scope: str | None = None

    # --- OAuth ----------------------------------------------------------------
    @abstractmethod
    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    @abstractmethod
    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet: ...

    @abstractmethod
    async def refresh(self, refresh_token: str) -> TokenSet: ...

    @abstractmethod
    async def revoke(self, token: str) -> None: ...

    # --- Mailbox --------------------------------------------------------------
    @abstractmethod
    async def get_profile(self, access_token: str) -> MailboxProfile: ...

    async def initial_cursor(self, access_token: str) -> str | None:
        """Cursor marking "now", taken before an initial sync so no message is missed."""
        return (await self.get_profile(access_token)).cursor

    @abstractmethod
    async def list_recent_message_ids(
        self, access_token: str, *, newer_than_days: int, max_messages: int
    ) -> list[str]: ...

    @abstractmethod
    async def list_changes(self, access_token: str, cursor: str) -> ChangeSet: ...

    @abstractmethod
    async def get_message(self, access_token: str, message_id: str) -> ProviderMessage: ...

    @abstractmethod
    async def send_reply(self, access_token: str, reply: OutgoingReply) -> str | None:
        """Send a reply; returns the provider's sent message id when available."""
