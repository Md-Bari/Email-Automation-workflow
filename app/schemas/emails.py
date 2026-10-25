from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models.enums import Priority, ProcessingStatus, ReplyTone
from app.schemas.common import ORMModel


class AnalysisOut(ORMModel):
    id: uuid.UUID
    email_id: uuid.UUID
    summary: str
    intent: str
    category: str
    priority: Priority
    is_emergency: bool
    emergency_reason: str | None
    emergency_evidence: list[str]
    suggested_action: str | None
    reply_recommended: bool
    confidence_score: float
    needs_review: bool
    review_reason: str | None
    input_truncated: bool
    decision_trace: dict[str, Any]
    model_name: str
    analysis_version: str
    created_at: datetime
    updated_at: datetime


class AnalysisBrief(ORMModel):
    priority: Priority
    is_emergency: bool
    needs_review: bool
    summary: str
    confidence_score: float


class EmailListItem(ORMModel):
    id: uuid.UUID
    email_account_id: uuid.UUID
    provider_thread_id: str | None
    sender_email: str
    sender_name: str | None
    subject: str
    snippet: str | None
    received_at: datetime
    is_read: bool
    is_automated: bool
    processing_status: ProcessingStatus
    has_attachments: bool = False
    analysis: AnalysisBrief | None = None


class EmailDetail(EmailListItem):
    reply_to: str | None
    recipients: dict[str, Any]
    body_text: str | None
    body_truncated: bool
    provider_labels: list[str]
    attachment_metadata: list[dict[str, Any]]
    processing_error: str | None
    provider_deleted_at: datetime | None
    created_at: datetime


class AnalyzeRequest(BaseModel):
    force: bool = Field(True, description="Re-run even if an up-to-date analysis exists.")


class GenerateReplyRequest(BaseModel):
    tone: ReplyTone | None = None
    instructions: str | None = Field(
        default=None, max_length=2000, description="Optional guidance from you, e.g. 'politely decline'."
    )


class MarkReadRequest(BaseModel):
    is_read: bool = True
