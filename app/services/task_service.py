"""BackgroundTask bookkeeping: creation + dispatch, worker lifecycle updates, listing and manual retry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ServiceUnavailableError
from app.core.logging import correlation_id_var, get_logger
from app.db.base import utcnow
from app.db.models import BackgroundTask
from app.db.models.enums import TaskStatus, TaskType
from app.services import dispatcher as d

logger = get_logger(__name__)

TASK_ROUTES: dict[TaskType, str] = {
    TaskType.INITIAL_SYNC: d.SYNC_ACCOUNT,
    TaskType.INCREMENTAL_SYNC: d.SYNC_ACCOUNT,
    TaskType.ANALYZE_EMAIL: d.ANALYZE_EMAIL,
    TaskType.GENERATE_DRAFT: d.GENERATE_DRAFT,
    TaskType.DELIVER_NOTIFICATION: d.DELIVER_NOTIFICATION,
    TaskType.CLEANUP: d.CLEANUP,
}
MANUALLY_RETRYABLE = {
    TaskType.INITIAL_SYNC, TaskType.INCREMENTAL_SYNC, TaskType.ANALYZE_EMAIL, TaskType.GENERATE_DRAFT,
}
ACTIVE_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRYING)


async def find_active(session: AsyncSession, task_type_in: tuple[TaskType, ...], resource_id: uuid.UUID
                      ) -> BackgroundTask | None:
    return (
        await session.execute(
            select(BackgroundTask)
            .where(
                BackgroundTask.task_type.in_(task_type_in),
                BackgroundTask.resource_id == resource_id,
                BackgroundTask.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_and_dispatch(
    session: AsyncSession,
    *,
    task_type: TaskType,
    user_id: uuid.UUID | None,
    resource_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    email_account_id: uuid.UUID | None = None,
) -> BackgroundTask:
    """Persist a task record, commit, then hand it to the broker (the worker must see the row)."""
    task = BackgroundTask(
        user_id=user_id,
        email_account_id=email_account_id,
        task_type=task_type,
        resource_id=resource_id,
        payload=payload or {},
        status=TaskStatus.QUEUED,
        correlation_id=correlation_id_var.get(),
    )
    session.add(task)
    await session.commit()
    _dispatch(task)
    return task


def _dispatch(task: BackgroundTask) -> None:
    try:
        d.get_dispatcher().send(TASK_ROUTES[task.task_type], {"task_id": str(task.id)}, task_id=str(task.id))
    except Exception as exc:  # broker unavailable
        logger.error("task_dispatch_failed", task_id=str(task.id), error=type(exc).__name__)
        raise ServiceUnavailableError("Background job queue is unavailable", code="queue_unavailable") from exc


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> BackgroundTask | None:
    return await session.get(BackgroundTask, task_id)


async def get_owned_task(session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> BackgroundTask:
    task = await session.get(BackgroundTask, task_id)
    if task is None or task.user_id != user_id:
        raise NotFoundError("Task not found")
    return task


async def list_tasks(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: TaskStatus | None,
    task_type: TaskType | None,
    offset: int,
    limit: int,
) -> tuple[list[BackgroundTask], int]:
    query = select(BackgroundTask).where(BackgroundTask.user_id == user_id)
    if status:
        query = query.where(BackgroundTask.status == status)
    if task_type:
        query = query.where(BackgroundTask.task_type == task_type)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await session.execute(query.order_by(BackgroundTask.created_at.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return list(rows), int(total)


async def retry_task(session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> BackgroundTask:
    settings = get_settings()
    task = await get_owned_task(session, task_id, user_id)
    if task.status != TaskStatus.FAILED:
        raise ConflictError("Only failed tasks can be retried", code="task_not_failed")
    if task.task_type not in MANUALLY_RETRYABLE:
        raise ConflictError("This task type cannot be retried manually", code="task_not_retryable")
    if task.manual_retries >= settings.task_manual_retry_limit:
        raise ConflictError("Manual retry limit reached", code="retry_limit_reached")
    if task.resource_id and await find_active(session, (task.task_type,), task.resource_id):
        raise ConflictError("An equivalent task is already running", code="task_already_running")
    task.status = TaskStatus.QUEUED
    task.manual_retries += 1
    task.attempts = 0
    task.error_code = None
    task.error_message = None
    task.progress = 0
    task.completed_at = None
    task.correlation_id = correlation_id_var.get()
    await session.commit()
    _dispatch(task)
    return task


# --- Worker-side lifecycle -----------------------------------------------------------


async def mark_running(session: AsyncSession, task: BackgroundTask) -> None:
    task.status = TaskStatus.RUNNING
    task.attempts += 1
    task.started_at = task.started_at or utcnow()
    await session.commit()


async def set_progress(session: AsyncSession, task: BackgroundTask, progress: int) -> None:
    task.progress = max(0, min(100, progress))
    await session.commit()


async def mark_succeeded(session: AsyncSession, task: BackgroundTask, result: dict[str, Any] | None) -> None:
    task.status = TaskStatus.SUCCEEDED
    task.progress = 100
    task.result = result
    task.error_code = None
    task.error_message = None
    task.completed_at = utcnow()
    await session.commit()


async def mark_failed(
    session: AsyncSession, task: BackgroundTask, *, code: str, message: str, will_retry: bool
) -> None:
    task.status = TaskStatus.RETRYING if will_retry else TaskStatus.FAILED
    task.error_code = code[:64]
    task.error_message = message[:500]
    if not will_retry:
        task.completed_at = utcnow()
    await session.commit()


def is_stale(task: BackgroundTask, older_than: datetime) -> bool:
    return task.status in ACTIVE_STATUSES and task.updated_at < older_than
