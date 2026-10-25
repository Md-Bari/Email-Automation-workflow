from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, Timestamps, UTCDateTime, UUIDPrimaryKey, str_enum
from app.db.models.enums import Channel, DeliveryStatus, NotificationType, ReplyTone, Severity


class Notification(UUIDPrimaryKey, CreatedAt, Base):
    """Persistent in-app notification. External deliveries are tracked in ``notification_deliveries``."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "deduplication_key", name="uq_notifications_user_dedup"),
        Index("ix_notifications_user_read", "user_id", "is_read", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    notification_type: Mapped[NotificationType] = mapped_column(
        str_enum(NotificationType, "notification_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "severity"), nullable=False)
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"), default=Channel.IN_APP, nullable=False)
    # Aggregate of external deliveries: delivered once all channels are delivered or skipped.
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"), default=DeliveryStatus.DELIVERED, nullable=False
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)

    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        back_populates="notification", cascade="all, delete-orphan", passive_deletes=True
    )


class NotificationDelivery(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "channel", name="uq_notification_deliveries_channel"),
        Index("ix_notification_deliveries_status", "status"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"), default=DeliveryStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    notification: Mapped[Notification] = relationship(back_populates="deliveries")


class NotificationPreference(UUIDPrimaryKey, Timestamps, Base):
    """User-level defaults (email_account_id NULL) with optional per-account overrides."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        Index(
            "uq_notification_preferences_user_default",
            "user_id",
            unique=True,
            postgresql_where=text("email_account_id IS NULL"),
            sqlite_where=text("email_account_id IS NULL"),
        ),
        Index(
            "uq_notification_preferences_user_account",
            "user_id",
            "email_account_id",
            unique=True,
            postgresql_where=text("email_account_id IS NOT NULL"),
            sqlite_where=text("email_account_id IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE")
    )
    emergency_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_needs_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notification_email: Mapped[str | None] = mapped_column(String(320))
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    auto_generate_drafts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_reply_tone: Mapped[ReplyTone] = mapped_column(
        str_enum(ReplyTone, "reply_tone"), default=ReplyTone.PROFESSIONAL, nullable=False
    )
