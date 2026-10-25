from __future__ import annotations

from celery import shared_task

from app.workers import jobs
from app.workers.base import RETRY_OPTIONS, run_async


@shared_task(name="tasks.deliver_notification", acks_late=True, **{**RETRY_OPTIONS, "retry_backoff": 10})
def deliver_notification(delivery_id: str, correlation_id: str | None = None) -> None:
    run_async(jobs.run_delivery_job(delivery_id), correlation_id)
