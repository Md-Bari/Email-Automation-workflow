from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import correlation_id_var, get_logger, redact
from app.db.models import AuditLog
from app.db.models.enums import AuditOutcome

logger = get_logger("audit")


def record_audit(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    user_id: uuid.UUID | None = None,
    email_account_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | str | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit entry to the session (committed together with the action it describes)."""
    entry = AuditLog(
        user_id=user_id,
        email_account_id=email_account_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        outcome=outcome,
        correlation_id=correlation_id_var.get(),
        ip_address=ip_address,
        details=redact(details or {}),
    )
    session.add(entry)
    logger.info("audit", action=action, resource_type=resource_type, outcome=outcome.value,
                user_id=str(user_id) if user_id else None)
    return entry
