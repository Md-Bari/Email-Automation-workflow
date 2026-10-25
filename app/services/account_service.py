"""Email account connection (OAuth + PKCE), credential management and disconnection."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.encryption import get_cipher
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailed
from app.core.kvstore import KeyValueStore
from app.core.logging import get_logger
from app.core.security import generate_opaque_token, generate_pkce_pair, hash_token
from app.db.base import utcnow
from app.db.models import EmailAccount, User
from app.db.models.enums import AuditOutcome, ConnectionStatus, EmailProvider, TaskType
from app.integrations.email_providers.base import (
    CredentialsRevokedError,
    EmailProviderClient,
    InsufficientScopeError,
    ProviderAuthError,
    TokenSet,
)
from app.integrations.email_providers.factory import get_provider_factory
from app.integrations.email_providers.sandbox import SANDBOX_TOKEN
from app.services import task_service
from app.services.audit_service import record_audit

logger = get_logger(__name__)
T = TypeVar("T")

OAUTH_STATE_PREFIX = "oauth-state:"
REFRESH_MARGIN = timedelta(seconds=90)


class OAuthCallbackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def list_accounts(session: AsyncSession, user_id: uuid.UUID) -> list[EmailAccount]:
    rows = await session.execute(
        select(EmailAccount).where(EmailAccount.user_id == user_id).order_by(EmailAccount.created_at)
    )
    return list(rows.scalars().all())


async def get_owned_account(session: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID) -> EmailAccount:
    account = await session.get(EmailAccount, account_id)
    if account is None or account.user_id != user_id:
        # 404 (not 403) so callers cannot probe other users' account ids.
        raise NotFoundError("Account not found")
    return account


def provider_for(account: EmailAccount) -> EmailProviderClient:
    return get_provider_factory().get(account.provider, mailbox=account.email_address)


def can_send(account: EmailAccount) -> bool:
    client = provider_for(account)
    return bool(client.send_scope and client.send_scope in account.granted_scopes)


# --- OAuth -----------------------------------------------------------------------------


async def start_oauth(store: KeyValueStore, user: User, provider: EmailProvider) -> str:
    if provider == EmailProvider.SANDBOX:
        raise ValidationFailed("Sandbox accounts are connected via /accounts/sandbox/connect")
    settings = get_settings()
    client = get_provider_factory().get(provider)
    state = generate_opaque_token(32)
    verifier, challenge = generate_pkce_pair()
    # Only a digest of the state is used as key; the verifier never leaves the server.
    await store.set(
        OAUTH_STATE_PREFIX + hash_token(state),
        json.dumps({"user_id": str(user.id), "provider": provider.value, "verifier": verifier}),
        settings.oauth_state_ttl_seconds,
    )
    return client.authorization_url(
        state=state, code_challenge=challenge, redirect_uri=settings.oauth_redirect_uri(provider.value)
    )


async def complete_oauth(
    session: AsyncSession,
    store: KeyValueStore,
    *,
    provider: EmailProvider,
    state: str | None,
    code: str | None,
    error: str | None,
    ip: str | None = None,
) -> EmailAccount:
    settings = get_settings()
    if not state:
        raise OAuthCallbackError("invalid_state", "Missing OAuth state")
    # pop() makes the state single-use, so a replayed callback is rejected.
    raw = await store.pop(OAUTH_STATE_PREFIX + hash_token(state))
    if raw is None:
        raise OAuthCallbackError("invalid_state", "OAuth state is invalid, expired or already used")
    data = json.loads(raw)
    if data["provider"] != provider.value:
        raise OAuthCallbackError("provider_mismatch", "OAuth callback provider does not match the request")
    user = await session.get(User, uuid.UUID(data["user_id"]))
    if user is None or not user.is_active:
        raise OAuthCallbackError("invalid_state", "Initiating user no longer exists")
    if error:
        record_audit(session, action="account.connect", resource_type="email_account",
                     outcome=AuditOutcome.FAILURE, user_id=user.id, ip_address=ip,
                     details={"provider": provider.value, "error": error[:100]})
        await session.commit()
        raise OAuthCallbackError("access_denied", "Authorization was not granted")
    if not code:
        raise OAuthCallbackError("missing_code", "Missing authorization code")

    client = get_provider_factory().get(provider)
    tokens = await client.exchange_code(
        code=code, code_verifier=data["verifier"], redirect_uri=settings.oauth_redirect_uri(provider.value)
    )
    if client.read_scope and client.read_scope not in tokens.scopes:
        await client.revoke(tokens.refresh_token or tokens.access_token)
        raise OAuthCallbackError("insufficient_scope", "Read access to the mailbox was not granted")

    # The mailbox identity comes from the provider API, never from user input.
    profile = await client.get_profile(tokens.access_token)
    existing = (
        await session.execute(
            select(EmailAccount).where(
                EmailAccount.provider == provider, EmailAccount.email_address == profile.email_address
            )
        )
    ).scalar_one_or_none()
    if existing and existing.user_id != user.id:
        record_audit(session, action="account.connect", resource_type="email_account",
                     outcome=AuditOutcome.DENIED, user_id=user.id, ip_address=ip,
                     details={"provider": provider.value, "reason": "mailbox_linked_to_other_user"})
        await session.commit()
        raise OAuthCallbackError("account_in_use", "This mailbox is already connected to another user")

    account = existing or EmailAccount(user_id=user.id, provider=provider, email_address=profile.email_address)
    _store_tokens(account, tokens, keep_refresh=existing is not None)
    account.connection_status = ConnectionStatus.ACTIVE
    account.last_error = None
    if existing is None:
        session.add(account)
    await session.flush()
    record_audit(session, action="account.connect", resource_type="email_account", resource_id=account.id,
                 user_id=user.id, email_account_id=account.id, ip_address=ip,
                 details={"provider": provider.value, "scopes": tokens.scopes, "reconnect": existing is not None})
    await session.commit()
    await enqueue_sync(session, account)
    return account


async def connect_sandbox(session: AsyncSession, user: User) -> EmailAccount:
    settings = get_settings()
    if not settings.sandbox_mode:
        raise ValidationFailed("Sandbox mode is disabled", code="provider_disabled")
    address = f"sandbox-{user.id.hex[:12]}@sandbox.local"
    existing = (
        await session.execute(
            select(EmailAccount).where(
                EmailAccount.provider == EmailProvider.SANDBOX, EmailAccount.email_address == address
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("Sandbox account already connected", code="already_connected")
    client = get_provider_factory().get(EmailProvider.SANDBOX, mailbox=address)
    account = EmailAccount(user_id=user.id, provider=EmailProvider.SANDBOX, email_address=address)
    _store_tokens(
        account,
        TokenSet(access_token=SANDBOX_TOKEN, refresh_token=SANDBOX_TOKEN, expires_at=None,
                 scopes=[s for s in (client.read_scope, client.send_scope) if s]),
        keep_refresh=False,
    )
    session.add(account)
    await session.flush()
    record_audit(session, action="account.connect", resource_type="email_account", resource_id=account.id,
                 user_id=user.id, email_account_id=account.id, details={"provider": "sandbox"})
    await session.commit()
    await enqueue_sync(session, account)
    return account


def _store_tokens(account: EmailAccount, tokens: TokenSet, *, keep_refresh: bool) -> None:
    cipher = get_cipher()
    account.encrypted_access_token = cipher.encrypt(tokens.access_token)
    # Providers often omit the refresh token on refresh/reconnect; keep the existing one then.
    if tokens.refresh_token or not keep_refresh:
        account.encrypted_refresh_token = cipher.encrypt(tokens.refresh_token)
    account.token_expiry = tokens.expires_at
    if tokens.scopes:
        account.granted_scopes = sorted(set(tokens.scopes))


async def enqueue_sync(session: AsyncSession, account: EmailAccount):
    """Queue a sync unless one is already queued/running for this account."""
    existing = await task_service.find_active(
        session, (TaskType.INITIAL_SYNC, TaskType.INCREMENTAL_SYNC), account.id
    )
    if existing:
        return existing
    task_type = TaskType.INCREMENTAL_SYNC if account.synchronization_cursor else TaskType.INITIAL_SYNC
    return await task_service.create_and_dispatch(
        session, task_type=task_type, user_id=account.user_id, resource_id=account.id,
        email_account_id=account.id, payload={"account_id": str(account.id)},
    )


# --- Credentials -------------------------------------------------------------------------


async def ensure_access_token(session: AsyncSession, account: EmailAccount, *, force_refresh: bool = False) -> str:
    if account.connection_status in (ConnectionStatus.REVOKED, ConnectionStatus.REAUTH_REQUIRED):
        raise CredentialsRevokedError()
    cipher = get_cipher()
    access = cipher.decrypt(account.encrypted_access_token)
    expiring = account.token_expiry is not None and account.token_expiry - REFRESH_MARGIN <= utcnow()
    if access and not force_refresh and not expiring:
        return access
    refresh_token = cipher.decrypt(account.encrypted_refresh_token)
    if not refresh_token:
        await mark_reauth_required(session, account, "No refresh token available")
        raise CredentialsRevokedError()
    try:
        tokens = await provider_for(account).refresh(refresh_token)
    except CredentialsRevokedError:
        await mark_reauth_required(session, account, "Refresh token revoked or expired")
        raise
    _store_tokens(account, tokens, keep_refresh=True)
    await session.commit()
    return tokens.access_token


async def mark_reauth_required(session: AsyncSession, account: EmailAccount, reason: str) -> None:
    account.connection_status = ConnectionStatus.REAUTH_REQUIRED
    account.last_error = reason[:500]
    record_audit(session, action="account.credentials_revoked", resource_type="email_account",
                 resource_id=account.id, user_id=account.user_id, email_account_id=account.id,
                 outcome=AuditOutcome.FAILURE, details={"reason": reason})
    await session.commit()


async def with_token(
    session: AsyncSession, account: EmailAccount, op: Callable[[str], Awaitable[T]]
) -> T:
    """Run a provider call with a valid token, refreshing once if the provider rejects it."""
    token = await ensure_access_token(session, account)
    try:
        return await op(token)
    except ProviderAuthError:
        token = await ensure_access_token(session, account, force_refresh=True)
        try:
            return await op(token)
        except ProviderAuthError as exc:
            await mark_reauth_required(session, account, "Access token rejected after refresh")
            raise CredentialsRevokedError() from exc
    except InsufficientScopeError:
        account.last_error = "Required permission not granted; reconnect the account"
        await session.commit()
        raise


async def disconnect(session: AsyncSession, account: EmailAccount, ip: str | None = None) -> None:
    """Revoke provider credentials (best effort) and delete the account with all its mailbox data."""
    cipher = get_cipher()
    try:
        token = cipher.decrypt(account.encrypted_refresh_token) or cipher.decrypt(account.encrypted_access_token)
        if token:
            await provider_for(account).revoke(token)
    except Exception as exc:  # never block disconnection on revocation failures
        logger.warning("token_revocation_failed", account_id=str(account.id), error=type(exc).__name__)
    record_audit(session, action="account.disconnect", resource_type="email_account", resource_id=account.id,
                 user_id=account.user_id, email_account_id=None, ip_address=ip,
                 details={"provider": account.provider.value})
    await session.delete(account)
    await session.commit()


def ensure_active(account: EmailAccount) -> None:
    if account.connection_status != ConnectionStatus.ACTIVE:
        raise PermissionDeniedError(
            "The account must be reconnected before it can be used", code="account_not_active"
        )
