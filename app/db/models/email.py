from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, Timestamps, UTCDateTime, UUIDPrimaryKey, str_enum
from app.db.models.enums import Priority, ProcessingStatus


class Email(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("email_account_id", "provider_message_id", name="uq_emails_account_message"),
        Index("ix_emails_user_received", "user_id", "received_at"),
        Index("ix_emails_account_thread", "email_account_id", "provider_thread_id"),
        Index("ix_emails_status", "processing_status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_thread_id: Mapped[str | None] = mapped_column(String(255))
    # RFC 5322 Message-ID / References, needed to thread replies correctly.
    internet_message_id: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[str | None] = mapped_column(Text)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    sender_name: Mapped[str | None] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    # {"to": [...], "cc": [...]} of {"email", "name"} objects
    recipients: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    subject: Mapped[str] = mapped_column(String(998), default="", nullable=False)
    snippet: Mapped[str | None] = mapped_column(String(500))
    # Readable text only (HTML is converted). May be cleared by the retention policy.
    body_text: Mapped[str | None] = mapped_column(Text)
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    provider_labels: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    attachment_metadata: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    # Bulk/automated mail signals (List-Unsubscribe, Precedence, no-reply sender).
    is_automated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        str_enum(ProcessingStatus, "processing_status"), default=ProcessingStatus.PENDING, nullable=False
    )
    processing_error: Mapped[str | None] = mapped_column(String(500))
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    analysis: Mapped[EmailAnalysis | None] = relationship(
        back_populates="email", uselist=False, cascade="all, delete-orphan", passive_deletes=True,
        lazy="selectin",
    )


class EmailAnalysis(UUIDPrimaryKey, Timestamps, Base):
    """Current AI analysis of an email (re-analysis replaces it; ``analysis_version`` tracks logic)."""

    __tablename__ = "email_analyses"
    __table_args__ = (
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1", name="confidence_range"),
        Index("ix_email_analyses_emergency", "is_emergency"),
        Index("ix_email_analyses_priority", "priority"),
    )

    email_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[Priority] = mapped_column(str_enum(Priority, "priority"), nullable=False)
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emergency_reason: Mapped[str | None] = mapped_column(Text)
    emergency_evidence: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    reply_recommended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_reason: Mapped[str | None] = mapped_column(String(500))
    # Deterministic rule signals and policy adjustments, for transparency/debugging.
    decision_trace: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    input_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(32), nullable=False)

    email: Mapped[Email] = relationship(back_populates="analysis", lazy="raise")
