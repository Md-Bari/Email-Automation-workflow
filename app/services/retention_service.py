"""Retention policy and housekeeping (run periodically by the scheduler)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import AuditLog, BackgroundTask, Email, EmailDraft, Notification, RefreshToken
from app.db.models.enums import DraftStatus, ProcessingStatus, TaskStatus

logger = get_logger(__name__)
STUCK_SEND_MINUTES = 15
STUCK_TASK_MINUTES = 30


async def run_cleanup(session: AsyncSession) -> dict[str, int]:
    s = get_settings()
    now = utcnow()
    stats: dict[str, int] = {}

    if s.email_body_retention_days:
        res = await session.execute(
            update(Email)
            .where(Email.received_at < now - timedelta(days=s.email_body_retention_days), Email.body_text.is_not(None))
            .values(body_text=None)
        )
        stats["email_bodies_cleared"] = res.rowcount or 0
    if s.email_retention_days:
        res = await session.execute(
            delete(Email).where(Email.received_at < now - timedelta(days=s.email_retention_days)))
        stats["emails_deleted"] = res.rowcount or 0
    if s.audit_log_retention_days:
        res = await session.execute(
            delete(AuditLog).where(AuditLog.created_at < now - timedelta(days=s.audit_log_retention_days)))
        stats["audit_logs_deleted"] = res.rowcount or 0
    if s.task_retention_days:
        res = await session.execute(
            delete(BackgroundTask).where(
                BackgroundTask.created_at < now - timedelta(days=s.task_retention_days),
                BackgroundTask.status.in_([TaskStatus.SUCCEEDED, TaskStatus.FAILED]),
            ))
        stats["tasks_deleted"] = res.rowcount or 0
    if s.notification_retention_days:
        res = await session.execute(
            delete(Notification).where(
                Notification.created_at < now - timedelta(days=s.notification_retention_days),
                Notification.is_read.is_(True),
            ))
        stats["notifications_deleted"] = res.rowcount or 0

    res = await session.execute(
        delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=1)))
    stats["refresh_tokens_deleted"] = res.rowcount or 0

    # A worker crash mid-send leaves a draft in "sending"; the outcome is unknown, so never auto-resend.
    res = await session.execute(
        update(EmailDraft)
        .where(EmailDraft.status == DraftStatus.SENDING,
               EmailDraft.updated_at < now - timedelta(minutes=STUCK_SEND_MINUTES))
        .values(status=DraftStatus.FAILED, error_code="send_outcome_unknown",
                error_message="Send did not complete; check the mailbox before retrying")
    )
    stats["stuck_sends_flagged"] = res.rowcount or 0

    res = await session.execute(
        update(BackgroundTask)
        .where(BackgroundTask.status.in_([TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.RETRYING]),
               BackgroundTask.updated_at < now - timedelta(minutes=STUCK_TASK_MINUTES))
        .values(status=TaskStatus.FAILED, error_code="stale_task",
                error_message="Task did not report progress and was marked failed", completed_at=now)
    )
    stats["stale_tasks_failed"] = res.rowcount or 0

    res = await session.execute(
        update(Email)
        .where(Email.processing_status == ProcessingStatus.PROCESSING,
               Email.updated_at < now - timedelta(minutes=STUCK_TASK_MINUTES))
        .values(processing_status=ProcessingStatus.PENDING)
    )
    stats["stuck_analyses_reset"] = res.rowcount or 0

    await session.commit()
    logger.info("cleanup_completed", **stats)
    return stats


async def emails_needing_analysis(session: AsyncSession, max_attempts: int, limit: int = 200) -> list:
    rows = await session.execute(
        select(Email.id)
        .where(Email.processing_status == ProcessingStatus.PENDING, Email.analysis_attempts < max_attempts,
               Email.provider_deleted_at.is_(None),
               Email.created_at < utcnow() - timedelta(minutes=2))
        .order_by(Email.received_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
