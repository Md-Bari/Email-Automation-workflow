"""Celery application: queues, time limits, retry defaults and the beat schedule."""

from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)

celery_app = Celery("email_assistant", broker=settings.broker_url, backend=settings.result_backend)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],  # never pickle (unsafe deserialization)
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.task_soft_time_limit_seconds,
    task_time_limit=settings.task_time_limit_seconds,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",
    # Each queue gets its own exchange + routing key; otherwise every queue would bind to the default
    # routing key and a single message would be delivered to all queues (duplicate execution).
    task_queues=tuple(
        Queue(name, Exchange(name, type="direct"), routing_key=name)
        for name in ("default", "sync", "analysis", "notifications", "maintenance")
    ),
    beat_schedule={
        "schedule-due-syncs": {"task": "tasks.schedule_due_syncs", "schedule": 60.0,
                               "options": {"queue": "maintenance"}},
        "retry-pending-analyses": {"task": "tasks.retry_pending_analyses", "schedule": 300.0,
                                   "options": {"queue": "maintenance"}},
        "retry-pending-deliveries": {"task": "tasks.retry_pending_deliveries", "schedule": 300.0,
                                     "options": {"queue": "maintenance"}},
        "cleanup": {"task": "tasks.cleanup", "schedule": 6 * 3600.0, "options": {"queue": "maintenance"}},
    },
)

# Importing the task modules registers their @shared_task functions with this app.
from app.workers import email_tasks, notification_tasks, scheduled_tasks  # noqa: E402,F401
