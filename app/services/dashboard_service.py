from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import AuditLog, BackgroundTask, Email, EmailAccount, EmailAnalysis, EmailDraft, Notification
from app.db.models.enums import DraftStatus, Priority, ProcessingStatus, TaskStatus


async def _count(session: AsyncSession, query) -> int:
    return int((await session.execute(query)).scalar_one())


async def summary(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    since = utcnow() - timedelta(days=7)
    analyzed = (
        select(func.count()).select_from(Email).join(EmailAnalysis, EmailAnalysis.email_id == Email.id)
        .where(Email.user_id == user_id)
    )
    return {
        "accounts": await _count(session, select(func.count()).select_from(EmailAccount)
                                 .where(EmailAccount.user_id == user_id)),
        "emails_total": await _count(session, select(func.count()).select_from(Email).where(Email.user_id == user_id)),
        "emails_processed": await _count(session, select(func.count()).select_from(Email).where(
            Email.user_id == user_id, Email.processing_status == ProcessingStatus.ANALYZED)),
        "emails_pending": await _count(session, select(func.count()).select_from(Email).where(
            Email.user_id == user_id,
            Email.processing_status.in_([ProcessingStatus.PENDING, ProcessingStatus.PROCESSING]))),
        "emails_failed": await _count(session, select(func.count()).select_from(Email).where(
            Email.user_id == user_id, Email.processing_status == ProcessingStatus.FAILED)),
        "urgent_emails": await _count(session, analyzed.where(EmailAnalysis.priority == Priority.URGENT)),
        "emergencies_last_7_days": await _count(session, analyzed.where(
            EmailAnalysis.is_emergency.is_(True), Email.received_at >= since)),
        "needs_review": await _count(session, analyzed.where(EmailAnalysis.needs_review.is_(True))),
        "pending_drafts": await _count(session, select(func.count()).select_from(EmailDraft).where(
            EmailDraft.user_id == user_id,
            EmailDraft.status.in_([DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED]))),
        "unread_notifications": await _count(session, select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id, Notification.is_read.is_(False))),
        "failed_jobs": await _count(session, select(func.count()).select_from(BackgroundTask).where(
            BackgroundTask.user_id == user_id, BackgroundTask.status == TaskStatus.FAILED)),
    }


async def activity(session: AsyncSession, user_id: uuid.UUID, limit: int) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(AuditLog.user_id == user_id).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    return list(rows.scalars().all())
