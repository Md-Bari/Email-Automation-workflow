"""Job implementations (async, broker-independent). Celery tasks are thin wrappers around these.

Every job is idempotent and safe to retry: sync deduplicates on (account, message id), analysis
upserts, deliveries skip non-pending rows, and draft sends are never retried automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, IntegrationError, TransientError
from app.core.kvstore import KeyValueStore, get_kv_store
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import BackgroundTask, Email, EmailAccount, EmailDraft, NotificationDelivery
from app.db.models.enums import ConnectionStatus, DeliveryStatus, ReplyTone, TaskStatus
from app.db.session import worker_session
from app.integrations.llm.factory import get_llm_provider
from app.services import (
    account_service,
    analysis_service,
    draft_service,
    notification_service,
    retention_service,
    sync_service,
    task_service,
)
from app.services.dispatcher import DELIVER_NOTIFICATION, get_dispatcher

logger = get_logger(__name__)
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class RetryableJobError(Exception):
    """Raised to ask Celery to retry the job with exponential backoff."""


async def _load_task(session: AsyncSession, task_id: str) -> BackgroundTask | None:
    task = await session.get(BackgroundTask, uuid.UUID(task_id))
    if task is None:
        logger.warning("task_record_missing", task_id=task_id)
    elif task.status == TaskStatus.SUCCEEDED:
        logger.info("task_already_succeeded", task_id=task_id)
        return None
    return task


async def _fail(session: AsyncSession, task: BackgroundTask, exc: Exception) -> None:
    settings = get_settings()
    await session.rollback()
    await session.refresh(task)  # rollback expires loaded objects; reload before reading attributes
    retryable = isinstance(exc, TransientError) or getattr(exc, "retryable", False)
    will_retry = retryable and task.attempts <= settings.task_max_retries
    code = getattr(exc, "code", "job_failed")
    await task_service.mark_failed(session, task, code=code, message=str(exc), will_retry=will_retry)
    if will_retry:
        raise RetryableJobError(code) from exc


async def run_sync_job(
    task_id: str, *, session_factory: SessionFactory = worker_session, store: KeyValueStore | None = None
) -> dict[str, Any] | None:
    store = store or get_kv_store()
    async with session_factory() as session:
        task = await _load_task(session, task_id)
        if task is None:
            return None
        await task_service.mark_running(session, task)
        account_id = uuid.UUID(task.payload["account_id"])

        async def progress(pct: int) -> None:
            await task_service.set_progress(session, task, pct)

        try:
            result = await sync_service.sync_account(session, store, account_id, progress=progress)
        except ConflictError as exc:
            await session.rollback()
            await session.refresh(task)
            await task_service.mark_failed(session, task, code=exc.code, message=exc.message, will_retry=False)
            return None
        except (IntegrationError, AppError) as exc:
            await _fail(session, task, exc)
            return None
        await task_service.mark_succeeded(session, task, result.as_dict())
        return result.as_dict()


async def run_analysis_job(
    email_id: str, *, force: bool = False, task_id: str | None = None,
    session_factory: SessionFactory = worker_session,
) -> str:
    async with session_factory() as session:
        task = await _load_task(session, task_id) if task_id else None
        if task:
            await task_service.mark_running(session, task)
        outcome = await analysis_service.run_analysis(session, uuid.UUID(email_id), force=force)
        if task:
            if outcome.status in ("analyzed", "skipped"):
                await task_service.mark_succeeded(session, task, {"status": outcome.status,
                                                                  "draft_id": outcome.state.get("draft_id")})
            else:
                await task_service.mark_failed(session, task, code=outcome.error_code or "analysis_failed",
                                               message=outcome.status, will_retry=outcome.retryable)
        if outcome.retryable:
            raise RetryableJobError(outcome.error_code or "analysis_failed")
        return outcome.status


async def run_generate_draft_job(task_id: str, *, session_factory: SessionFactory = worker_session) -> str | None:
    async with session_factory() as session:
        task = await _load_task(session, task_id)
        if task is None:
            return None
        await task_service.mark_running(session, task)
        payload = task.payload
        llm = get_llm_provider()
        try:
            email = await session.get(Email, uuid.UUID(payload["email_id"]))
            if email is None or email.user_id != task.user_id:
                raise AppError("Email no longer exists", code="email_not_found")
            account = await session.get(EmailAccount, email.email_account_id)
            tone = ReplyTone(payload.get("tone") or ReplyTone.PROFESSIONAL)
            reply = await draft_service.generate_reply(
                session, llm, email, tone=tone,
                summary=email.analysis.summary if email.analysis else None,
                instructions=payload.get("instructions"),
            )
            if payload.get("draft_id"):
                draft = await session.get(EmailDraft, uuid.UUID(payload["draft_id"]))
                if draft is None or draft.user_id != task.user_id:
                    raise AppError("Draft no longer exists", code="draft_not_found")
                draft_service.apply_regenerated(draft, reply, tone=tone, model=llm.model_name)
            else:
                draft = await draft_service.create_draft_from_reply(
                    session, email, account, reply, tone=tone, model=llm.model_name
                )
            await session.commit()
        except (IntegrationError, AppError) as exc:
            await _fail(session, task, exc)
            return None
        await task_service.mark_succeeded(session, task, {"draft_id": str(draft.id)})
        return str(draft.id)


async def run_delivery_job(delivery_id: str, *, session_factory: SessionFactory = worker_session) -> None:
    async with session_factory() as session:
        try:
            await notification_service.deliver(session, uuid.UUID(delivery_id))
        except TransientError as exc:
            raise RetryableJobError(exc.code) from exc


async def run_cleanup_job(*, session_factory: SessionFactory = worker_session) -> dict[str, int]:
    async with session_factory() as session:
        return await retention_service.run_cleanup(session)


async def schedule_due_syncs(*, session_factory: SessionFactory = worker_session) -> int:
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=settings.sync_interval_seconds)
    count = 0
    async with session_factory() as session:
        accounts = (
            await session.execute(
                select(EmailAccount).where(
                    EmailAccount.connection_status == ConnectionStatus.ACTIVE,
                    or_(EmailAccount.last_synced_at.is_(None), EmailAccount.last_synced_at < cutoff),
                )
            )
        ).scalars().all()
        for account in accounts:
            try:
                await account_service.enqueue_sync(session, account)
                count += 1
            except AppError as exc:
                logger.warning("scheduled_sync_enqueue_failed", account_id=str(account.id), error=exc.code)
    return count


async def retry_pending_analyses(*, session_factory: SessionFactory = worker_session) -> int:
    settings = get_settings()
    async with session_factory() as session:
        ids = await retention_service.emails_needing_analysis(session, settings.analysis_max_attempts)
    for email_id in ids:
        sync_service.enqueue_analysis(email_id)
    return len(ids)


async def retry_pending_deliveries(*, session_factory: SessionFactory = worker_session) -> int:
    async with session_factory() as session:
        rows = await session.execute(
            select(NotificationDelivery.id).where(
                NotificationDelivery.status == DeliveryStatus.PENDING,
                NotificationDelivery.updated_at < utcnow() - timedelta(minutes=10),
            ).limit(200)
        )
        ids = list(rows.scalars().all())
    for delivery_id in ids:
        get_dispatcher().send(DELIVER_NOTIFICATION, {"delivery_id": str(delivery_id)})
    return len(ids)
