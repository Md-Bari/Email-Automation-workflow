from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models.enums import ConnectionStatus, EmailProvider
from app.schemas.common import ORMModel
from app.schemas.tasks import TaskOut


class AccountOut(ORMModel):
    """Public view of a connected account. Credentials are never included."""

    id: uuid.UUID
    provider: EmailProvider
    email_address: str
    connection_status: ConnectionStatus
    granted_scopes: list[str]
    can_send: bool = False
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime


class ConnectResponse(BaseModel):
    authorization_url: str
    expires_in: int


class SyncStatusOut(BaseModel):
    account_id: uuid.UUID
    connection_status: ConnectionStatus
    last_synced_at: datetime | None
    incremental_sync_ready: bool
    active_task: TaskOut | None
    last_task: TaskOut | None


class OAuthResult(BaseModel):
    status: str
    account_id: uuid.UUID | None = None
    error_code: str | None = None
    message: str | None = None
