"""Mailbox synchronization: bounded initial sync, incremental sync via cursors, deduplication."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.kvstore import KeyValueStore
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import Email, EmailAccount
from app.db.models.enums import ConnectionStatus, ProcessingStatus
from app.integrations.email_providers.base import (
    CursorExpiredError,
    MessageNotFoundError,
    ProviderMessage,
)
from app.integrations.email_providers.parsing import truncate
from app.observability import metrics
from app.services import account_service
from app.services.dispatcher import ANALYZE_EMAIL, get_dispatcher

logger = get_logger(__name__)
ProgressCallback = Callable[[int], Awaitable[None]]
COMMIT_EVERY = 25


@dataclass(slots=True)
class SyncResult:
    mode: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    deleted: int = 0
    skipped_missing: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def email_from_provider(account: EmailAccount, msg: ProviderMessage, max_body_chars: int) -> Email:
    body, was_truncated = truncate(msg.body_text or "", max_body_chars)
    return Email(
        user_id=account.user_id,
        email_account_id=account.id,
        provider_message_id=msg.provider_message_id,
        provider_thread_id=msg.thread_id,
        internet_message_id=(msg.internet_message_id or "")[:998] or None,
        references=msg.references,
        sender_email=msg.sender.email[:320],
        sender_name=(msg.sender.name or "")[:255] or None,
        reply_to=msg.reply_to,
        recipients={"to": [a.as_dict() for a in msg.to], "cc": [a.as_dict() for a in msg.cc]},
        subject=msg.subject[:998],
        snippet=msg.snippet,
        body_text=body,
        body_truncated=was_truncated,
        received_at=msg.received_at,
        provider_labels=msg.labels,
        attachment_metadata=msg.attachments,
        is_automated=msg.is_automated,
        processing_status=ProcessingStatus.PENDING,
    )


async def sync_account(
    session: AsyncSession,
    store: KeyValueStore,
    account_id: uuid.UUID,
    *,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    settings = get_settings()
    account = await session.get(EmailAccount, account_id)
    if account is None:
        raise NotFoundError("Account not found")
    account_service.ensure_active(account)

    lock_key = f"sync-lock:{account.id}"
    if not await store.acquire_lock(lock_key, settings.task_time_limit_seconds):
        raise ConflictError("A synchronization is already running for this account", code="sync_in_progress")
    try:
        result = await _run_sync(session, account, progress)
    finally:
        await store.delete(lock_key)
    metrics.EMAILS_SYNCED.labels(provider=account.provider.value).inc(result.inserted)
    logger.info("sync_completed", account_id=str(account.id), **result.as_dict())
    return result


async def _run_sync(session: AsyncSession, account: EmailAccount, progress: ProgressCallback | None) -> SyncResult:
    settings = get_settings()
    client = account_service.provider_for(account)

    async def call(fn):
        return await account_service.with_token(session, account, fn)

    deleted_ids: list[str] = []
    if account.synchronization_cursor:
        try:
            changes = await call(lambda t: client.list_changes(t, account.synchronization_cursor or ""))
            result = SyncResult(mode="incremental")
            message_ids, deleted_ids, new_cursor = changes.added_ids, changes.deleted_ids, changes.new_cursor
        except CursorExpiredError:
            # History expired: resync only the window since the last successful sync (bounded).
            since = account.last_synced_at or (utcnow() - timedelta(days=settings.initial_sync_days))
            days = max(1, min(settings.initial_sync_days, (utcnow() - since).days + 1))
            new_cursor = await call(client.initial_cursor)
            message_ids = await call(lambda t: client.list_recent_message_ids(
                t, newer_than_days=days, max_messages=settings.initial_sync_max_messages))
            result = SyncResult(mode="resync_after_cursor_expiry")
    else:
        # Take the cursor *before* listing so mail arriving mid-sync is picked up next time.
        new_cursor = await call(client.initial_cursor)
        message_ids = await call(lambda t: client.list_recent_message_ids(
            t, newer_than_days=settings.initial_sync_days, max_messages=settings.initial_sync_max_messages))
        result = SyncResult(mode="initial")

    result.fetched = len(message_ids)
    existing = await _existing_ids(session, account.id, message_ids)
    new_ids = [mid for mid in message_ids if mid not in existing]
    result.duplicates = len(message_ids) - len(new_ids)
    inserted: list[uuid.UUID] = []

    for index, message_id in enumerate(new_ids, start=1):
        try:
            msg = await call(lambda t, mid=message_id: client.get_message(t, mid))
        except MessageNotFoundError:
            result.skipped_missing += 1
            continue
        email = email_from_provider(account, msg, settings.max_body_chars)
        try:
            async with session.begin_nested():
                session.add(email)
            inserted.append(email.id)
        except IntegrityError:
            result.duplicates += 1  # concurrent insert of the same message
        if index % COMMIT_EVERY == 0:
            await session.commit()
            if progress:
                await progress(int(index / max(len(new_ids), 1) * 90))
    result.inserted = len(inserted)

    if deleted_ids:
        res = await session.execute(
            update(Email)
            .where(Email.email_account_id == account.id, Email.provider_message_id.in_(deleted_ids),
                   Email.provider_deleted_at.is_(None))
            .values(provider_deleted_at=utcnow())
        )
        result.deleted = res.rowcount or 0

    # The checkpoint advances only after every message of this batch is stored.
    account.synchronization_cursor = new_cursor
    account.last_synced_at = utcnow()
    account.last_error = None
    if account.connection_status == ConnectionStatus.ERROR:
        account.connection_status = ConnectionStatus.ACTIVE
    await session.commit()

    for email_id in inserted:
        enqueue_analysis(email_id)
    return result


async def _existing_ids(session: AsyncSession, account_id: uuid.UUID, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = await session.execute(
            select(Email.provider_message_id).where(
                Email.email_account_id == account_id, Email.provider_message_id.in_(chunk)
            )
        )
        found.update(rows.scalars().all())
    return found


def enqueue_analysis(email_id: uuid.UUID, *, force: bool = False) -> None:
    """Automatic analysis jobs are tracked on the email row (processing_status), not as BackgroundTasks."""
    try:
        get_dispatcher().send(ANALYZE_EMAIL, {"email_id": str(email_id), "force": force})
    except Exception as exc:
        # The periodic retry job picks up emails left in 'pending'.
        logger.warning("analysis_dispatch_failed", email_id=str(email_id), error=type(exc).__name__)
