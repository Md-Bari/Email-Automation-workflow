from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.db.models.enums import Channel, DeliveryStatus, NotificationType, ReplyTone, Severity
from app.schemas.common import ORMModel


class DeliveryOut(ORMModel):
    channel: Channel
    status: DeliveryStatus
    attempts: int
    delivered_at: datetime | None


class NotificationOut(ORMModel):
    id: uuid.UUID
    email_id: uuid.UUID | None
    notification_type: NotificationType
    title: str
    message: str
    severity: Severity
    channel: Channel
    delivery_status: DeliveryStatus
    is_read: bool
    acknowledged_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime


class NotificationDetail(NotificationOut):
    deliveries: list[DeliveryOut] = []


class PreferencesOut(BaseModel):
    email_account_id: uuid.UUID | None
    emergency_alerts_enabled: bool
    notify_on_needs_review: bool
    email_enabled: bool
    notification_email: str | None
    telegram_enabled: bool
    telegram_chat_id: str | None
    auto_generate_drafts: bool
    default_reply_tone: ReplyTone
    available_channels: list[Channel]


class PreferencesUpdate(BaseModel):
    emergency_alerts_enabled: bool | None = None
    notify_on_needs_review: bool | None = None
    email_enabled: bool | None = None
    notification_email: EmailStr | None = None
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = Field(default=None, pattern=r"^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")
    auto_generate_drafts: bool | None = None
    default_reply_tone: ReplyTone | None = None


class ReadAllResponse(BaseModel):
    updated: int
