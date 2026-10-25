from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, JSONType, Timestamps, UTCDateTime, UUIDPrimaryKey, str_enum
from app.db.models.enums import AuditOutcome, TaskStatus, TaskType


class BackgroundTask(UUIDPrimaryKey, Timestamps, Base):
    """User-visible record of an asynchronous job. Its id doubles as the Celery task id."""

    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="progress_range"),
        Index("ix_background_tasks_user_created", "user_id", "created_at"),
        Index("ix_background_tasks_status", "status"),
        Index("ix_background_tasks_resource", "task_type", "resource_id", "status"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE")
    )
    task_type: Mapped[TaskType] = mapped_column(str_enum(TaskType, "task_type"), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, "task_status"), default=TaskStatus.QUEUED, nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    manual_retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditLog(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    # SET NULL keeps the security trail (without personal data) after a user deletes their account.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    email_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[AuditOutcome] = mapped_column(str_enum(AuditOutcome, "audit_outcome"), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
