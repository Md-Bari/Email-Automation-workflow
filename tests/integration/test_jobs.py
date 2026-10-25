"""Background job lifecycle: status tracking, retries with limits, recovery and scheduling.

Jobs run in their own session (as in a real worker). The test session ends its transaction
(``commit``) before handing over and re-reads afterwards, like a separate process would.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.db.base import utcnow
from app.db.models import BackgroundTask, Email, EmailAccount
from app.db.models.enums import ConnectionStatus, EmailProvider, TaskStatus, TaskType
from app.integrations.email_providers.base import TokenSet
from app.services import account_service, task_service
from app.services.dispatcher import SYNC_ACCOUNT
from app.workers import jobs


async def reload(session, obj):
    await session.commit()
    await session.refresh(obj)
    return obj


async def sync_task(session) -> BackgroundTask:
    task = (await session.execute(select(BackgroundTask).where(BackgroundTask.task_type == TaskType.INITIAL_SYNC))
            ).scalars().first()
    await session.commit()
    return task


async def test_sync_job_records_success(session, sessionmaker, store, sandbox_account, dispatcher):
    task = await sync_task(session)
    assert task.status == TaskStatus.QUEUED and dispatcher.sent[0][0] == SYNC_ACCOUNT
    result = await jobs.run_sync_job(str(task.id), session_factory=sessionmaker, store=store)
    assert result["inserted"] == 7
    await reload(session, task)
    assert task.status == TaskStatus.SUCCEEDED and task.progress == 100 and task.attempts == 1
    # Re-delivery of an already-succeeded job is a no-op.
    assert await jobs.run_sync_job(str(task.id), session_factory=sessionmaker, store=store) is None


@pytest.fixture
async def failing_gmail_task(session, user, provider_factory, dispatcher):
    provider_factory.gmail.unavailable = True
    account = EmailAccount(user_id=user.id, provider=EmailProvider.GMAIL, email_address="owner@gmail.com",
                           granted_scopes=[])
    account_service._store_tokens(account, TokenSet("a", "r", None, []), keep_refresh=False)
    session.add(account)
    await session.commit()
    task = await account_service.enqueue_sync(session, account)
    await session.commit()
    return task


async def test_transient_failure_retries_then_fails_at_limit(session, sessionmaker, store, failing_gmail_task,
                                                            monkeypatch):
    monkeypatch.setattr(get_settings(), "task_max_retries", 1)
    task_id = str(failing_gmail_task.id)
    with pytest.raises(jobs.RetryableJobError):
        await jobs.run_sync_job(task_id, session_factory=sessionmaker, store=store)
    await reload(session, failing_gmail_task)
    assert failing_gmail_task.status == TaskStatus.RETRYING
    assert failing_gmail_task.error_code == "provider_unavailable"
    # Second attempt exhausts the retry budget: final state is FAILED and no retry is requested.
    await jobs.run_sync_job(task_id, session_factory=sessionmaker, store=store)
    await reload(session, failing_gmail_task)
    assert failing_gmail_task.status == TaskStatus.FAILED and failing_gmail_task.attempts == 2


async def test_manual_retry_rules(session, sessionmaker, store, failing_gmail_task, user, monkeypatch,
                                  provider_factory):
    monkeypatch.setattr(get_settings(), "task_max_retries", 0)
    monkeypatch.setattr(get_settings(), "task_manual_retry_limit", 1)
    with pytest.raises(ConflictError):  # not failed yet
        await task_service.retry_task(session, failing_gmail_task.id, user.id)
    await session.commit()
    await jobs.run_sync_job(str(failing_gmail_task.id), session_factory=sessionmaker, store=store)
    await reload(session, failing_gmail_task)
    assert failing_gmail_task.status == TaskStatus.FAILED

    with pytest.raises(NotFoundError):  # other users cannot retry it
        await task_service.retry_task(session, failing_gmail_task.id, uuid.uuid4())

    provider_factory.gmail.unavailable = False
    retried = await task_service.retry_task(session, failing_gmail_task.id, user.id)
    assert retried.status == TaskStatus.QUEUED and retried.manual_retries == 1
    await jobs.run_sync_job(str(retried.id), session_factory=sessionmaker, store=store)
    await reload(session, retried)
    assert retried.status == TaskStatus.SUCCEEDED

    retried.status = TaskStatus.FAILED
    await session.commit()
    with pytest.raises(ConflictError):  # manual retry limit reached
        await task_service.retry_task(session, retried.id, user.id)


async def test_schedule_due_syncs_skips_inactive_and_running(session, sessionmaker, sandbox_account, dispatcher):
    # The sandbox account already has a queued initial sync -> not scheduled twice.
    dispatcher.clear()
    await session.commit()
    await jobs.schedule_due_syncs(session_factory=sessionmaker)
    assert dispatcher.names() == []
    task = await sync_task(session)
    task.status = TaskStatus.SUCCEEDED
    sandbox_account.connection_status = ConnectionStatus.REAUTH_REQUIRED
    await session.commit()
    await jobs.schedule_due_syncs(session_factory=sessionmaker)
    assert dispatcher.names() == []
    sandbox_account.connection_status = ConnectionStatus.ACTIVE
    await session.commit()
    assert await jobs.schedule_due_syncs(session_factory=sessionmaker) == 1
    assert dispatcher.names() == [SYNC_ACCOUNT]


async def test_cleanup_applies_retention(session, sessionmaker, store, sandbox_account):
    await jobs.run_sync_job(str((await sync_task(session)).id), session_factory=sessionmaker, store=store)
    old = (await session.execute(select(Email).limit(1))).scalar_one()
    old.received_at = utcnow() - timedelta(days=45)
    await session.commit()
    stats = await jobs.run_cleanup_job(session_factory=sessionmaker)
    assert stats["email_bodies_cleared"] == 1
    await reload(session, old)
    assert old.body_text is None
