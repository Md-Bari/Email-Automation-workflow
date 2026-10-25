from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import CurrentUser, DbSession, Pagination
from app.core.config import get_settings
from app.db.models import NotificationDelivery
from app.db.models.enums import Channel, NotificationType
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.notifications import (
    DeliveryOut,
    NotificationDetail,
    NotificationOut,
    PreferencesOut,
    PreferencesUpdate,
    ReadAllResponse,
)
from app.services import account_service, notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"], responses=ERROR_RESPONSES)


def _available_channels() -> list[Channel]:
    s = get_settings()
    channels = [Channel.IN_APP]
    if s.smtp_host and s.smtp_from:
        channels.append(Channel.EMAIL)
    if s.telegram_bot_token:
        channels.append(Channel.TELEGRAM)
    return channels


@router.get("", response_model=Page[NotificationOut])
async def list_notifications(
    user: CurrentUser, session: DbSession, pagination: Pagination,
    is_read: bool | None = None, notification_type: NotificationType | None = None,
) -> Page[NotificationOut]:
    rows, total = await notification_service.list_notifications(
        session, user.id, is_read=is_read, notification_type=notification_type,
        offset=pagination.offset, limit=pagination.page_size,
    )
    return Page[NotificationOut].build([NotificationOut.model_validate(n) for n in rows], total,
                                       pagination.page, pagination.page_size)


# Declared before /{notification_id} routes so the literal paths take precedence.
@router.post("/read-all", response_model=ReadAllResponse)
async def read_all(user: CurrentUser, session: DbSession) -> ReadAllResponse:
    return ReadAllResponse(updated=await notification_service.mark_all_read(session, user.id))


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(user: CurrentUser, session: DbSession, account_id: uuid.UUID | None = None
                          ) -> PreferencesOut:
    if account_id:
        await account_service.get_owned_account(session, account_id, user.id)
    prefs = await notification_service.effective_preferences(session, user.id, account_id)
    return PreferencesOut(email_account_id=account_id, available_channels=_available_channels(),
                          **{f: getattr(prefs, f) for f in notification_service.PREF_FIELDS})


@router.patch("/preferences", response_model=PreferencesOut)
async def update_preferences(
    body: PreferencesUpdate, user: CurrentUser, session: DbSession, account_id: uuid.UUID | None = None
) -> PreferencesOut:
    """Update user-wide defaults, or per-account overrides when ``account_id`` is given."""
    if account_id:
        await account_service.get_owned_account(session, account_id, user.id)
    changes = body.model_dump(exclude_unset=True)
    prefs = await notification_service.update_preferences(session, user.id, account_id, changes)
    return PreferencesOut(email_account_id=account_id, available_channels=_available_channels(),
                          **{f: getattr(prefs, f) for f in notification_service.PREF_FIELDS})


@router.get("/{notification_id}", response_model=NotificationDetail)
async def get_notification(notification_id: uuid.UUID, user: CurrentUser, session: DbSession) -> NotificationDetail:
    notification = await notification_service.get_owned(session, notification_id, user.id)
    query = select(NotificationDelivery).where(NotificationDelivery.notification_id == notification.id)
    deliveries = (await session.execute(query)).scalars().all()
    return NotificationDetail(
        **NotificationOut.model_validate(notification).model_dump(),
        deliveries=[DeliveryOut.model_validate(d) for d in deliveries],
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: uuid.UUID, user: CurrentUser, session: DbSession) -> NotificationOut:
    notification = await notification_service.get_owned(session, notification_id, user.id)
    return NotificationOut.model_validate(await notification_service.mark_read(session, notification))


@router.post("/{notification_id}/acknowledge", response_model=NotificationOut)
async def acknowledge(notification_id: uuid.UUID, user: CurrentUser, session: DbSession) -> NotificationOut:
    notification = await notification_service.get_owned(session, notification_id, user.id)
    return NotificationOut.model_validate(await notification_service.acknowledge(session, notification))
