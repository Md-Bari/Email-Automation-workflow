from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from app.db.models import BackgroundTask
from app.db.session import worker_session
from app.workers import jobs
from app.workers.base import RETRY_OPTIONS, run_async


@shared_task(name="tasks.sync_account", acks_late=True, **RETRY_OPTIONS)
def sync_account(task_id: str, correlation_id: str | None = None) -> dict[str, Any] | None:
    return run_async(jobs.run_sync_job(task_id), correlation_id)


@shared_task(name="tasks.analyze_email", acks_late=True, **RETRY_OPTIONS)
def analyze_email(
    email_id: str | None = None, force: bool = False, task_id: str | None = None, correlation_id: str | None = None
) -> str | None:
    if email_id is None and task_id is not None:
        # Manually requested analysis: the email id lives in the BackgroundTask payload.
        return run_async(_analysis_from_task(task_id), correlation_id)
    return run_async(jobs.run_analysis_job(email_id or "", force=force), correlation_id)


async def _analysis_from_task(task_id: str) -> str | None:
    async with worker_session() as session:
        task = await session.get(BackgroundTask, uuid.UUID(task_id))
        if task is None:
            return None
        email_id, force = task.payload["email_id"], bool(task.payload.get("force", True))
    return await jobs.run_analysis_job(email_id, force=force, task_id=task_id)


@shared_task(name="tasks.generate_draft", acks_late=True, **RETRY_OPTIONS)
def generate_draft(task_id: str, correlation_id: str | None = None) -> str | None:
    return run_async(jobs.run_generate_draft_job(task_id), correlation_id)
