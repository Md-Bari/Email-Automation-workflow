from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.db.models.enums import DraftStatus, ReplyTone
from app.schemas.common import ORMModel


class DraftOut(ORMModel):
    id: uuid.UUID
    email_id: uuid.UUID
    draft_subject: str
    draft_body: str
    to_address: str
    tone: ReplyTone
    status: DraftStatus
    needs_clarification: bool
    is_ai_generated: bool
    model_name: str | None
    approved_at: datetime | None
    sent_at: datetime | None
    provider_sent_message_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DraftUpdate(BaseModel):
    draft_body: str | None = Field(default=None, min_length=1, max_length=50_000)
    draft_subject: str | None = Field(default=None, min_length=1, max_length=998)
    tone: ReplyTone | None = None

    @field_validator("draft_subject")
    @classmethod
    def _no_newlines(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("subject must be a single line")
        return value


class RegenerateRequest(BaseModel):
    tone: ReplyTone | None = None
    instructions: str | None = Field(default=None, max_length=2000)


class SendRequest(BaseModel):
    confirm: Literal[True] = Field(description="Must be true: explicit confirmation that you want to send.")


class SendResponse(BaseModel):
    draft: DraftOut
    replayed: bool = Field(description="True when this response repeats an earlier send (same Idempotency-Key).")
