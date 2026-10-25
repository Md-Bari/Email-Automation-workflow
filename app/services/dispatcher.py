"""Decouples services from Celery so business logic is testable without a broker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import correlation_id_var

# Celery task names and their queues.
SYNC_ACCOUNT = "tasks.sync_account"
ANALYZE_EMAIL = "tasks.analyze_email"
GENERATE_DRAFT = "tasks.generate_draft"
DELIVER_NOTIFICATION = "tasks.deliver_notification"
CLEANUP = "tasks.cleanup"

QUEUES = {
    SYNC_ACCOUNT: "sync",
    ANALYZE_EMAIL: "analysis",
    GENERATE_DRAFT: "analysis",
    DELIVER_NOTIFICATION: "notifications",
    CLEANUP: "maintenance",
}


class TaskDispatcher(ABC):
    @abstractmethod
    def send(self, name: str, kwargs: dict[str, Any], *, task_id: str | None = None) -> None: ...


class CeleryDispatcher(TaskDispatcher):
    def send(self, name: str, kwargs: dict[str, Any], *, task_id: str | None = None) -> None:
        from app.workers.celery_app import celery_app

        kwargs = {**kwargs, "correlation_id": kwargs.get("correlation_id") or correlation_id_var.get()}
        celery_app.send_task(name, kwargs=kwargs, task_id=task_id, queue=QUEUES.get(name, "default"))


@dataclass
class RecordingDispatcher(TaskDispatcher):
    """Test double that records dispatched tasks instead of sending them to a broker."""

    sent: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)

    def send(self, name: str, kwargs: dict[str, Any], *, task_id: str | None = None) -> None:
        self.sent.append((name, kwargs, task_id))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.sent]

    def clear(self) -> None:
        self.sent.clear()


_dispatcher: TaskDispatcher | None = None


def get_dispatcher() -> TaskDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = CeleryDispatcher()
    return _dispatcher


def set_dispatcher(dispatcher: TaskDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = dispatcher
