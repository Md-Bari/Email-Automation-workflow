"""Persistent notifications, per-channel delivery with retries, and user preferences."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, TransientError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import Email, EmailAnalysis, Notification, NotificationDelivery, NotificationPreference
from app.db.models.enums import Channel, DeliveryStatus, NotificationType, ReplyTone, Severity
from app.integrations.notifiers.base import NotifierNotConfigured, NotifierRegistry, OutboundNotification
from app.observability import metrics

logger = get_logger(__name__)


@dataclass(slots=True)
class EffectivePreferences:
    emergency_alerts_enabled: bool = True
    notify_on_needs_review: bool = True
    email_enabled: bool = False
    notification_email: str | None = None
    telegram_enabled: bool = False
    telegram_chat_id: str | None = None
    auto_generate_drafts: bool = True
    default_reply_tone: ReplyTone = ReplyTone.PROFESSIONAL


PREF_FIELDS = tuple(EffectivePreferences.__dataclass_fields__)


async def _pref_row(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID | None
) -> NotificationPreference | None:
    cond = (
        NotificationPreference.email_account_id.is_(None)
        if account_id is None
        else NotificationPreference.email_account_id == account_id
    )
    return (
        await session.execute(select(NotificationPreference).where(NotificationPreference.user_id == user_id, cond))
    ).scalar_one_or_none()


async def effective_preferences(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID | None = None
) -> EffectivePreferences:
    """Account-specific row overrides the user default row, which overrides built-in defaults."""
    row = (await _pref_row(session, user_id, account_id) if account_id else None) or await _pref_row(
        session, user_id, None
    )
    if row is None:
        return EffectivePreferences()
    return EffectivePreferences(**{f: getattr(row, f) for f in PREF_FIELDS})


async def update_preferences(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID | None, changes: dict[str, Any]
) -> EffectivePreferences:
    row = await _pref_row(session, user_id, account_id)
    if row is None:
        base = await effective_preferences(session, user_id, None)
        row = NotificationPreference(user_id=user_id, email_account_id=account_id,
                                     **{f: getattr(base, f) for f in PREF_FIELDS})
        session.add(row)
    for key, value in changes.items():
        if key in PREF_FIELDS:
            setattr(row, key, value)
    await session.commit()
    return await effective_preferences(session, user_id, account_id)


# --- Creation -----------------------------------------------------------------------------


def _compose(email: Email, analysis: EmailAnalysis, kind: NotificationType) -> tuple[str, str]:
    subject = (email.subject or "(no subject)")[:120]
    sender = f"{email.sender_name} <{email.sender_email}>" if email.sender_name else email.sender_email
    if kind == NotificationType.EMERGENCY:
        title = f"Emergency: {subject}"[:255]
        reason = analysis.emergency_reason or "Classified as an emergency"
    else:
        title = f"Needs review: {subject}"[:255]
        reason = analysis.review_reason or "Classification needs human review"
    message = (
        f"From: {sender}\n"
        f"Received: {email.received_at.isoformat()}\n"
        f"Reason: {reason}\n"
        f"Summary: {analysis.summary}\n"
        f"Suggested action: {analysis.suggested_action or 'Review the email'}"
    )
    return title, message


async def create_email_notification(
    session: AsyncSession, email: Email, analysis: EmailAnalysis, kind: NotificationType
) -> Notification | None:
    """Create a deduplicated notification plus delivery records. Returns None if it already exists."""
    prefs = await effective_preferences(session, email.user_id, email.email_account_id)
    if kind == NotificationType.EMERGENCY and not prefs.emergency_alerts_enabled:
        return None
    if kind == NotificationType.NEEDS_REVIEW and not prefs.notify_on_needs_review:
        return None
    dedup_key = f"{kind.value}:email:{email.id}"
    exists = (
        await session.execute(
            select(Notification.id).where(Notification.user_id == email.user_id,
                                          Notification.deduplication_key == dedup_key)
        )
    ).scalar_one_or_none()
    if exists:
        return None

    title, message = _compose(email, analysis, kind)
    notification = Notification(
        user_id=email.user_id, email_id=email.id, notification_type=kind, title=title, message=message,
        severity=Severity.CRITICAL if kind == NotificationType.EMERGENCY else Severity.WARNING,
        channel=Channel.IN_APP, delivery_status=DeliveryStatus.DELIVERED, delivered_at=utcnow(),
        deduplication_key=dedup_key,
    )
    try:
        async with session.begin_nested():
            session.add(notification)
    except IntegrityError:
        return None  # created concurrently by another worker

    # External channels only for emergencies; "needs review" stays in-app to avoid noise.
    if kind == NotificationType.EMERGENCY:
        channels: list[tuple[Channel, bool]] = [
            (Channel.EMAIL, prefs.email_enabled and bool(prefs.notification_email)),
            (Channel.TELEGRAM, prefs.telegram_enabled and bool(prefs.telegram_chat_id)),
        ]
        pending = [c for c, enabled in channels if enabled]
        for channel in pending:
            session.add(NotificationDelivery(notification_id=notification.id, channel=channel))
        if pending:
            notification.delivery_status = DeliveryStatus.PENDING
            notification.delivered_at = None
    await session.flush()
    return notification


# --- Delivery -----------------------------------------------------------------------------


async def pending_delivery_ids(session: AsyncSession, notification_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(NotificationDelivery.id).where(
            NotificationDelivery.notification_id == notification_id,
            NotificationDelivery.status == DeliveryStatus.PENDING,
        )
    )
    return list(rows.scalars().all())


async def deliver(session: AsyncSession, delivery_id: uuid.UUID, registry: NotifierRegistry | None = None) -> bool:
    """Attempt one delivery. Returns True when finished; raises TransientError when a retry is due."""
    settings = get_settings()
    delivery = await session.get(NotificationDelivery, delivery_id)
    if delivery is None or delivery.status != DeliveryStatus.PENDING:
        return True  # idempotent: already handled
    notification = await session.get(Notification, delivery.notification_id)
    if notification is None:
        return True
    email = await session.get(Email, notification.email_id) if notification.email_id else None
    prefs = await effective_preferences(session, notification.user_id, email.email_account_id if email else None)
    destination = prefs.notification_email if delivery.channel == Channel.EMAIL else prefs.telegram_chat_id
    payload = OutboundNotification(
        title=notification.title,
        text=notification.message,
        link=f"{settings.frontend_url.rstrip('/')}/emails/{notification.email_id}" if notification.email_id else None,
    )
    delivery.attempts += 1
    try:
        if not destination:
            raise NotifierNotConfigured(delivery.channel.value)
        notifier = (registry or NotifierRegistry(settings)).get(delivery.channel.value)
        await notifier.send(destination, payload)
    except TransientError as exc:
        delivery.last_error = str(exc)[:500]
        if delivery.attempts >= settings.notification_max_attempts:
            delivery.status = DeliveryStatus.FAILED
            await _refresh_aggregate(session, notification)
            await session.commit()
            metrics.NOTIFICATION_DELIVERIES.labels(channel=delivery.channel.value, outcome="failed").inc()
            return True
        await session.commit()
        raise
    except Exception as exc:  # non-retryable (rejected, not configured, bad destination)
        delivery.status = DeliveryStatus.SKIPPED if isinstance(exc, NotifierNotConfigured) else DeliveryStatus.FAILED
        delivery.last_error = str(exc)[:500]
        await _refresh_aggregate(session, notification)
        await session.commit()
        metrics.NOTIFICATION_DELIVERIES.labels(channel=delivery.channel.value, outcome=delivery.status.value).inc()
        return True
    delivery.status = DeliveryStatus.DELIVERED
    delivery.delivered_at = utcnow()
    delivery.last_error = None
    await _refresh_aggregate(session, notification)
    await session.commit()
    metrics.NOTIFICATION_DELIVERIES.labels(channel=delivery.channel.value, outcome="delivered").inc()
    return True


async def _refresh_aggregate(session: AsyncSession, notification: Notification) -> None:
    await session.flush()
    statuses = (
        await session.execute(
            select(NotificationDelivery.status).where(NotificationDelivery.notification_id == notification.id)
        )
    ).scalars().all()
    if any(s == DeliveryStatus.PENDING for s in statuses):
        notification.delivery_status = DeliveryStatus.PENDING
    elif any(s == DeliveryStatus.FAILED for s in statuses):
        notification.delivery_status = DeliveryStatus.FAILED
    else:
        notification.delivery_status = DeliveryStatus.DELIVERED
        notification.delivered_at = notification.delivered_at or utcnow()


# --- Queries / user actions -----------------------------------------------------------


async def get_owned(session: AsyncSession, notification_id: uuid.UUID, user_id: uuid.UUID) -> Notification:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotFoundError("Notification not found")
    return notification


async def list_notifications(
    session: AsyncSession, user_id: uuid.UUID, *, is_read: bool | None, notification_type: NotificationType | None,
    offset: int, limit: int,
) -> tuple[list[Notification], int]:
    query = select(Notification).where(Notification.user_id == user_id)
    if is_read is not None:
        query = query.where(Notification.is_read == is_read)
    if notification_type:
        query = query.where(Notification.notification_type == notification_type)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = await session.execute(query.order_by(Notification.created_at.desc()).offset(offset).limit(limit))
    return list(rows.scalars().all()), int(total)


async def mark_read(session: AsyncSession, notification: Notification) -> Notification:
    notification.is_read = True
    await session.commit()
    return notification


async def acknowledge(session: AsyncSession, notification: Notification) -> Notification:
    notification.is_read = True
    notification.acknowledged_at = notification.acknowledged_at or utcnow()
    await session.commit()
    return notification


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        update(Notification).where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    return result.rowcount or 0
