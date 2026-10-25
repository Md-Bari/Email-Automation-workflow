"""Synchronization: initial sync, incremental checkpoints, deduplication and failure handling."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.encryption import get_cipher
from app.core.exceptions import ConflictError, PermissionDeniedError
from app.db.models import Email, EmailAccount
from app.db.models.enums import ConnectionStatus, EmailProvider
from app.integrations.email_providers.base import CredentialsRevokedError, ProviderUnavailableError, TokenSet
from app.integrations.email_providers.sandbox import SandboxProvider, seed_messages
from app.services import account_service, sync_service
from app.services.dispatcher import ANALYZE_EMAIL


async def count_emails(session, account_id):
    return (await session.execute(select(func.count()).select_from(Email)
                                  .where(Email.email_account_id == account_id))).scalar_one()


async def test_initial_then_incremental_sync_without_duplicates(session, store, sandbox_account, dispatcher):
    dispatcher.clear()
    result = await sync_service.sync_account(session, store, sandbox_account.id)
    assert result.mode == "initial" and result.inserted == 7
    assert dispatcher.names().count(ANALYZE_EMAIL) == 7
    await session.refresh(sandbox_account)
    assert sandbox_account.synchronization_cursor == "sandbox:7"
    assert sandbox_account.last_synced_at is not None

    again = await sync_service.sync_account(session, store, sandbox_account.id)
    assert again.mode == "incremental" and again.inserted == 0
    assert await count_emails(session, sandbox_account.id) == 7


async def test_incremental_sync_picks_up_new_mail(session, store, sandbox_account):
    await sync_service.sync_account(session, store, sandbox_account.id)
    new = replace(seed_messages()[3], provider_message_id="sbx-100", subject="Follow-up")
    SandboxProvider.incoming[sandbox_account.email_address].append(new)
    result = await sync_service.sync_account(session, store, sandbox_account.id)
    assert result.mode == "incremental" and result.inserted == 1 and result.fetched == 1
    await session.refresh(sandbox_account)
    assert sandbox_account.synchronization_cursor == "sandbox:8"


async def test_expired_cursor_falls_back_to_bounded_resync(session, store, sandbox_account):
    await sync_service.sync_account(session, store, sandbox_account.id)
    sandbox_account.synchronization_cursor = "sandbox:999"  # beyond history -> expired
    await session.commit()
    result = await sync_service.sync_account(session, store, sandbox_account.id)
    assert result.mode == "resync_after_cursor_expiry"
    # Bounded window (since last sync, 1 day): older mail is not re-downloaded, nothing is duplicated.
    assert result.inserted == 0 and result.duplicates == result.fetched == 6
    await session.refresh(sandbox_account)
    assert sandbox_account.synchronization_cursor == "sandbox:7"


async def test_concurrent_sync_is_rejected_by_lock(session, store, sandbox_account):
    await store.acquire_lock(f"sync-lock:{sandbox_account.id}", 60)
    with pytest.raises(ConflictError):
        await sync_service.sync_account(session, store, sandbox_account.id)


async def gmail_account(session, user, *, expired: bool = False) -> EmailAccount:
    account = EmailAccount(user_id=user.id, provider=EmailProvider.GMAIL, email_address="owner@gmail.com",
                           granted_scopes=[])
    account_service._store_tokens(account, TokenSet("a", "r", None, []), keep_refresh=False)
    if expired:
        account.token_expiry = datetime(2000, 1, 1, tzinfo=UTC)
    session.add(account)
    await session.commit()
    return account


async def test_revoked_credentials_mark_account_for_reauth(session, store, user, provider_factory):
    account = await gmail_account(session, user, expired=True)
    provider_factory.gmail.refresh_fails = True
    with pytest.raises(CredentialsRevokedError):
        await sync_service.sync_account(session, store, account.id)
    await session.refresh(account)
    assert account.connection_status == ConnectionStatus.REAUTH_REQUIRED
    # A sync on a revoked account is refused up front.
    with pytest.raises(PermissionDeniedError):
        await sync_service.sync_account(session, store, account.id)


async def test_expired_access_token_is_refreshed_and_persisted(session, store, user, provider_factory):
    account = await gmail_account(session, user, expired=True)
    await sync_service.sync_account(session, store, account.id)
    await session.refresh(account)
    assert get_cipher().decrypt(account.encrypted_access_token) == "access-2"
    assert get_cipher().decrypt(account.encrypted_refresh_token) == "r"  # kept when provider omits it


async def test_provider_outage_does_not_advance_cursor(session, store, user, provider_factory):
    account = await gmail_account(session, user)
    provider_factory.gmail.unavailable = True
    with pytest.raises(ProviderUnavailableError):
        await sync_service.sync_account(session, store, account.id)
    await session.refresh(account)
    assert account.synchronization_cursor is None and account.last_synced_at is None
