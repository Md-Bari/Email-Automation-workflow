from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models.enums import AuditOutcome
from app.schemas.common import ORMModel


class DashboardSummary(BaseModel):
    accounts: int
    emails_total: int
    emails_processed: int
    emails_pending: int
    emails_failed: int
    urgent_emails: int
    emergencies_last_7_days: int
    needs_review: int
    pending_drafts: int
    unread_notifications: int
    failed_jobs: int


class ActivityItem(ORMModel):
    id: uuid.UUID
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    email_account_id: uuid.UUID | None
    created_at: datetime


class HealthOut(BaseModel):
    status: str


class ReadinessOut(BaseModel):
    status: str
    checks: dict[str, bool]
