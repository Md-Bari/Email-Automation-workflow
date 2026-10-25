"""Builds the LLM-facing view of an email (and its thread) from the database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Email

THREAD_LIMIT = 5
THREAD_EXCERPT_CHARS = 800


@dataclass(slots=True)
class EmailContext:
    data: dict[str, Any]
    body: str
    truncated: bool
    thread: list[dict[str, Any]]

    def mock_context(self, **extra: Any) -> dict[str, Any]:
        return {**self.data, "body": self.body, **extra}


def email_dict(email: Email) -> dict[str, Any]:
    return {
        "id": str(email.id),
        "sender_email": email.sender_email,
        "sender_name": email.sender_name,
        "to": email.recipients.get("to", []),
        "cc": email.recipients.get("cc", []),
        "subject": email.subject,
        "received_at": email.received_at.isoformat(),
        "is_automated": email.is_automated,
        "attachments": email.attachment_metadata,
    }


async def build_context(session: AsyncSession, email: Email, max_chars: int) -> EmailContext:
    body = email.body_text or email.snippet or ""
    truncated = email.body_truncated or len(body) > max_chars
    body = body[:max_chars]
    thread: list[dict[str, Any]] = []
    if email.provider_thread_id:
        rows = await session.execute(
            select(Email)
            .where(
                Email.email_account_id == email.email_account_id,
                Email.provider_thread_id == email.provider_thread_id,
                Email.id != email.id,
                Email.received_at <= email.received_at,
            )
            .order_by(Email.received_at.desc())
            .limit(THREAD_LIMIT)
        )
        for prior in reversed(rows.scalars().all()):
            thread.append({
                "sender_email": prior.sender_email,
                "received_at": prior.received_at.isoformat(),
                "excerpt": (prior.body_text or prior.snippet or "")[:THREAD_EXCERPT_CHARS],
            })
    return EmailContext(data=email_dict(email), body=body, truncated=truncated, thread=thread)
