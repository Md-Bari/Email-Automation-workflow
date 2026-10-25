from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.db.models.enums import TaskStatus, TaskType
from app.schemas.common import ORMModel


class TaskOut(ORMModel):
    id: uuid.UUID
    task_type: TaskType
    status: TaskStatus
    progress: int
    resource_id: uuid.UUID | None
    email_account_id: uuid.UUID | None
    attempts: int
    manual_retries: int
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskAccepted(ORMModel):
    """Returned with 202 Accepted for asynchronous operations."""

    task_id: uuid.UUID
    status: TaskStatus
    task_type: TaskType
    status_url: str
