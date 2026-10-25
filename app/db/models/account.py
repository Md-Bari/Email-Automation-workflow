from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UTCDateTime, UUIDPrimaryKey, str_enum
from app.db.models.enums import ConnectionStatus, EmailProvider


class EmailAccount(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "email_accounts"
    __table_args__ = (
        # A mailbox can be linked to only one application user (prevents account mix-up).
        UniqueConstraint("provider", "email_address", name="uq_email_accounts_provider_address"),
        Index("ix_email_accounts_status_synced", "connection_status", "last_synced_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[EmailProvider] = mapped_column(str_enum(EmailProvider, "email_provider"), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expiry: Mapped[datetime | None] = mapped_column(UTCDateTime)
    granted_scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    connection_status: Mapped[ConnectionStatus] = mapped_column(
        str_enum(ConnectionStatus, "connection_status"), default=ConnectionStatus.ACTIVE, nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Gmail historyId / Graph deltaLink. NULL means an initial (bounded) sync is required.
    synchronization_cursor: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(String(500))
