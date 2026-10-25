"""Email queries (filtering, pagination, threads) with strict per-user scoping."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import Email, EmailAnalysis
from app.db.models.enums import Priority, ProcessingStatus


@dataclass(slots=True)
class EmailFilters:
    account_id: uuid.UUID | None = None
    priority: Priority | None = None
    is_emergency: bool | None = None
    needs_review: bool | None = None
    processing_status: ProcessingStatus | None = None
    sender: str | None = None
    is_read: bool | None = None
    received_after: datetime | None = None
    received_before: datetime | None = None
    q: str | None = None
    include_deleted: bool = False


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(query: Select, f: EmailFilters) -> Select:
    if f.account_id:
        query = query.where(Email.email_account_id == f.account_id)
    if f.processing_status:
        query = query.where(Email.processing_status == f.processing_status)
    if f.sender:
        query = query.where(Email.sender_email.ilike(f"%{_escape_like(f.sender.lower())}%", escape="\\"))
    if f.is_read is not None:
        query = query.where(Email.is_read == f.is_read)
    if f.received_after:
        query = query.where(Email.received_at >= f.received_after)
    if f.received_before:
        query = query.where(Email.received_at < f.received_before)
    if f.q:
        query = query.where(Email.subject.ilike(f"%{_escape_like(f.q)}%", escape="\\"))
    if not f.include_deleted:
        query = query.where(Email.provider_deleted_at.is_(None))
    if f.priority or f.is_emergency is not None or f.needs_review is not None:
        query = query.join(EmailAnalysis, EmailAnalysis.email_id == Email.id)
        if f.priority:
            query = query.where(EmailAnalysis.priority == f.priority)
        if f.is_emergency is not None:
            query = query.where(EmailAnalysis.is_emergency == f.is_emergency)
        if f.needs_review is not None:
            query = query.where(EmailAnalysis.needs_review == f.needs_review)
    return query


async def list_emails(
    session: AsyncSession, user_id: uuid.UUID, filters: EmailFilters, *, offset: int, limit: int
) -> tuple[list[Email], int]:
    query = _apply_filters(select(Email).where(Email.user_id == user_id), filters)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = await session.execute(query.order_by(Email.received_at.desc(), Email.id).offset(offset).limit(limit))
    return list(rows.scalars().all()), int(total)


async def get_owned_email(session: AsyncSession, email_id: uuid.UUID, user_id: uuid.UUID) -> Email:
    email = await session.get(Email, email_id)
    if email is None or email.user_id != user_id:
        raise NotFoundError("Email not found")
    return email


async def get_thread(session: AsyncSession, email: Email) -> list[Email]:
    if not email.provider_thread_id:
        return [email]
    rows = await session.execute(
        select(Email)
        .where(
            Email.user_id == email.user_id,
            Email.email_account_id == email.email_account_id,
            Email.provider_thread_id == email.provider_thread_id,
        )
        .order_by(Email.received_at)
    )
    return list(rows.scalars().all())


async def set_read(session: AsyncSession, email: Email, is_read: bool) -> Email:
    email.is_read = is_read
    await session.commit()
    return email
