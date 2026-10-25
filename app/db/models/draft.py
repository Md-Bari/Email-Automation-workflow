from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UTCDateTime, UUIDPrimaryKey, str_enum
from app.db.models.enums import DraftStatus, ReplyTone


class EmailDraft(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "email_drafts"
    __table_args__ = (
        Index("ix_email_drafts_user_status", "user_id", "status"),
        Index("ix_email_drafts_email", "email_id"),
    )

    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    draft_subject: Mapped[str] = mapped_column(String(998), nullable=False)
    draft_body: Mapped[str] = mapped_column(Text, nullable=False)
    # Recipient is derived by application code from the original email, never by the LLM.
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    tone: Mapped[ReplyTone] = mapped_column(str_enum(ReplyTone, "reply_tone"), nullable=False)
    status: Mapped[DraftStatus] = mapped_column(
        str_enum(DraftStatus, "draft_status"), default=DraftStatus.PENDING_REVIEW, nullable=False
    )
    needs_clarification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128))
    # Hash of subject+body+recipient at approval time; sending requires it to still match.
    approved_content_hash: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    provider_sent_message_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
