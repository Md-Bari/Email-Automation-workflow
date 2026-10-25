"""Fake providers: an OAuth-capable fake Gmail and a factory wiring fakes into the services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.models.enums import EmailProvider
from app.integrations.email_providers.base import (
    ChangeSet,
    CredentialsRevokedError,
    EmailProviderClient,
    MailboxProfile,
    MessageNotFoundError,
    OutgoingReply,
    ProviderMessage,
    ProviderUnavailableError,
    TokenSet,
)
from app.integrations.email_providers.factory import ProviderFactory
from app.integrations.email_providers.gmail import GMAIL_READONLY, GMAIL_SEND
from app.integrations.email_providers.sandbox import SandboxProvider


class FakeGmail(EmailProviderClient):
    name = "gmail"
    read_scope = GMAIL_READONLY
    send_scope = GMAIL_SEND

    def __init__(self) -> None:
        self.mailbox = "owner@gmail.com"
        self.granted = [GMAIL_READONLY, GMAIL_SEND]
        self.exchanged: list[dict] = []
        self.revoked: list[str] = []
        self.refresh_fails = False
        self.unavailable = False
        self.sent: list[OutgoingReply] = []

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}&code_challenge={code_challenge}&redirect_uri={redirect_uri}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        self.exchanged.append({"code": code, "verifier": code_verifier, "redirect_uri": redirect_uri})
        return TokenSet(access_token="access-1", refresh_token="refresh-1",
                        expires_at=datetime.now(UTC) + timedelta(hours=1), scopes=list(self.granted))

    async def refresh(self, refresh_token: str) -> TokenSet:
        if self.refresh_fails:
            raise CredentialsRevokedError()
        return TokenSet(access_token="access-2", refresh_token=None,
                        expires_at=datetime.now(UTC) + timedelta(hours=1), scopes=list(self.granted))

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)

    async def get_profile(self, access_token: str) -> MailboxProfile:
        return MailboxProfile(email_address=self.mailbox, cursor="100")

    async def list_recent_message_ids(self, access_token: str, *, newer_than_days: int, max_messages: int) -> list[str]:
        if self.unavailable:
            raise ProviderUnavailableError()
        return []

    async def list_changes(self, access_token: str, cursor: str) -> ChangeSet:
        return ChangeSet(added_ids=[], deleted_ids=[], new_cursor=cursor)

    async def get_message(self, access_token: str, message_id: str) -> ProviderMessage:
        raise MessageNotFoundError(message_id)

    async def send_reply(self, access_token: str, reply: OutgoingReply) -> str | None:
        self.sent.append(reply)
        return "gmail-sent-1"


class TestProviderFactory(ProviderFactory):
    __test__ = False

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.gmail = FakeGmail()

    def get(self, provider: EmailProvider, mailbox: str | None = None) -> EmailProviderClient:
        if provider == EmailProvider.GMAIL:
            return self.gmail
        return SandboxProvider(mailbox)
