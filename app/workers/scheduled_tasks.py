from __future__ import annotations

from celery import shared_task

from app.workers import jobs
from app.workers.base import run_async


@shared_task(name="tasks.schedule_due_syncs")
def schedule_due_syncs(correlation_id: str | None = None) -> int:
    return run_async(jobs.schedule_due_syncs(), correlation_id)


@shared_task(name="tasks.retry_pending_analyses")
def retry_pending_analyses(correlation_id: str | None = None) -> int:
    return run_async(jobs.retry_pending_analyses(), correlation_id)


@shared_task(name="tasks.retry_pending_deliveries")
def retry_pending_deliveries(correlation_id: str | None = None) -> int:
    return run_async(jobs.retry_pending_deliveries(), correlation_id)


@shared_task(name="tasks.cleanup")
def cleanup(task_id: str | None = None, correlation_id: str | None = None) -> dict[str, int]:
    return run_async(jobs.run_cleanup_job(), correlation_id)
