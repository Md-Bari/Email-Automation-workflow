"""Reply drafts: generation, editing, validated state transitions, approval and safe sending.

Safety invariants enforced here (never delegated to the LLM):
* drafts are never sent automatically; sending requires an approved draft + explicit confirmation;
* approval is bound to the exact content (hash); any edit invalidates it;
* the recipient is derived from the original email, never from model output;
* an idempotency key plus a compare-and-set state change prevents duplicate sends.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.context import build_context
from app.agents.prompts.templates import REPLY_SYSTEM, reply_user_prompt
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import Email, EmailAccount, EmailDraft
from app.db.models.enums import AuditOutcome, DraftStatus, ReplyTone
from app.integrations.email_providers.base import (
    CredentialsRevokedError,
    InsufficientScopeError,
    IntegrationError,
    OutgoingReply,
    ProviderRequestError,
)
from app.integrations.email_providers.parsing import is_noreply_address, reply_subject
from app.integrations.llm.base import LLMProvider
from app.integrations.llm.schemas import ReplyDraft
from app.observability import metrics
from app.services import account_service
from app.services.audit_service import record_audit

logger = get_logger(__name__)

TRANSITIONS: dict[DraftStatus, set[DraftStatus]] = {
    DraftStatus.PENDING_REVIEW: {DraftStatus.APPROVED, DraftStatus.REJECTED},
    DraftStatus.APPROVED: {DraftStatus.SENDING, DraftStatus.PENDING_REVIEW, DraftStatus.REJECTED},
    DraftStatus.SENDING: {DraftStatus.SENT, DraftStatus.FAILED},
    DraftStatus.FAILED: {DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED, DraftStatus.REJECTED},
    DraftStatus.REJECTED: {DraftStatus.PENDING_REVIEW},
    DraftStatus.SENT: set(),
}
EDITABLE = {DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED, DraftStatus.FAILED, DraftStatus.REJECTED}
PLACEHOLDER = re.compile(r"\[[^\[\]\n]{2,80}\]")
SEND_OUTCOME_UNKNOWN = "send_outcome_unknown"


class SendFailedError(AppError):
    status_code = 502
    code = "send_failed"
    message = "The email provider did not accept the message"


def transition(draft: EmailDraft, new_status: DraftStatus) -> None:
    if new_status not in TRANSITIONS[draft.status]:
        raise InvalidStateTransition(
            f"Cannot move draft from '{draft.status.value}' to '{new_status.value}'",
            details={"from": draft.status.value, "to": new_status.value},
        )
    draft.status = new_status


def content_hash(draft: EmailDraft) -> str:
    material = "\x1f".join([draft.to_address, draft.draft_subject, draft.draft_body])
    return hashlib.sha256(material.encode()).hexdigest()


def find_placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER.findall(text)))


def reply_recipient(email: Email, account: EmailAccount) -> str:
    target = (email.reply_to or email.sender_email or "").strip().lower()
    try:
        target = validate_email(target, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValidationFailed("The original email has no valid reply address", code="invalid_recipient") from exc
    if is_noreply_address(target):
        raise ValidationFailed("The sender does not accept replies (no-reply address)", code="noreply_recipient")
    if target == account.email_address.lower():
        raise ValidationFailed("Cannot reply to your own address", code="invalid_recipient")
    return target


async def get_owned_draft(session: AsyncSession, draft_id: uuid.UUID, user_id: uuid.UUID) -> EmailDraft:
    draft = await session.get(EmailDraft, draft_id)
    if draft is None or draft.user_id != user_id:
        raise NotFoundError("Draft not found")
    return draft


async def list_drafts(
    session: AsyncSession, user_id: uuid.UUID, *, status: DraftStatus | None, email_id: uuid.UUID | None,
    offset: int, limit: int,
) -> tuple[list[EmailDraft], int]:
    query = select(EmailDraft).where(EmailDraft.user_id == user_id)
    if status:
        query = query.where(EmailDraft.status == status)
    if email_id:
        query = query.where(EmailDraft.email_id == email_id)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = await session.execute(query.order_by(EmailDraft.created_at.desc()).offset(offset).limit(limit))
    return list(rows.scalars().all()), int(total)


# --- Generation ---------------------------------------------------------------------------


async def generate_reply(
    session: AsyncSession,
    llm: LLMProvider,
    email: Email,
    *,
    tone: ReplyTone,
    summary: str | None = None,
    instructions: str | None = None,
) -> ReplyDraft:
    settings = get_settings()
    ctx = await build_context(session, email, settings.llm_max_input_chars)
    prompt = reply_user_prompt(ctx.data, ctx.body, ctx.truncated, ctx.thread, tone=tone.value,
                               summary=summary, instructions=instructions)
    return await llm.structured(
        system=REPLY_SYSTEM, user=prompt, schema=ReplyDraft,
        context=ctx.mock_context(tone=tone.value, instructions=instructions),
    )


async def create_draft_from_reply(
    session: AsyncSession, email: Email, account: EmailAccount, reply: ReplyDraft, *, tone: ReplyTone, model: str
) -> EmailDraft:
    draft = EmailDraft(
        email_id=email.id,
        user_id=email.user_id,
        draft_subject=reply_subject(email.subject)[:998],
        draft_body=reply.body,
        to_address=reply_recipient(email, account),
        tone=tone,
        status=DraftStatus.PENDING_REVIEW,
        needs_clarification=reply.needs_clarification or bool(reply.unresolved_placeholders),
        is_ai_generated=True,
        model_name=model,
    )
    session.add(draft)
    await session.flush()
    return draft


def apply_regenerated(draft: EmailDraft, reply: ReplyDraft, *, tone: ReplyTone, model: str) -> None:
    if draft.status not in EDITABLE:
        raise InvalidStateTransition(f"Cannot regenerate a draft in state '{draft.status.value}'")
    draft.draft_body = reply.body
    draft.tone = tone
    draft.model_name = model
    draft.is_ai_generated = True
    draft.needs_clarification = reply.needs_clarification or bool(reply.unresolved_placeholders)
    _reset_to_review(draft)


def _reset_to_review(draft: EmailDraft) -> None:
    if draft.status != DraftStatus.PENDING_REVIEW:
        transition(draft, DraftStatus.PENDING_REVIEW)
    draft.approved_at = None
    draft.approved_content_hash = None
    draft.idempotency_key = None
    draft.error_code = None
    draft.error_message = None


# --- Review actions -----------------------------------------------------------------------


async def update_draft(
    session: AsyncSession, draft: EmailDraft, *, body: str | None, subject: str | None, tone: ReplyTone | None
) -> EmailDraft:
    if draft.status not in EDITABLE:
        raise InvalidStateTransition(f"Cannot edit a draft in state '{draft.status.value}'")
    if body is not None:
        draft.draft_body = body
    if subject is not None:
        draft.draft_subject = subject
    if tone is not None:
        draft.tone = tone
    # Any edit invalidates a previous approval.
    _reset_to_review(draft)
    await session.commit()
    return draft


async def approve(session: AsyncSession, draft: EmailDraft, user_id: uuid.UUID, ip: str | None) -> EmailDraft:
    if draft.status == DraftStatus.FAILED and draft.error_code == SEND_OUTCOME_UNKNOWN:
        raise ConflictError(
            "The previous send attempt may have been delivered. Check the mailbox, then edit the draft "
            "before approving it again.", code="send_outcome_unknown",
        )
    placeholders = find_placeholders(draft.draft_body) + find_placeholders(draft.draft_subject)
    if placeholders:
        raise ValidationFailed("Fill in all placeholders before approving", code="unresolved_placeholders",
                               details={"placeholders": placeholders})
    transition(draft, DraftStatus.APPROVED)
    draft.approved_at = utcnow()
    draft.approved_content_hash = content_hash(draft)
    draft.idempotency_key = None
    draft.error_code = None
    draft.error_message = None
    email = await session.get(Email, draft.email_id)
    record_audit(session, action="draft.approve", resource_type="email_draft", resource_id=draft.id,
                 user_id=user_id, email_account_id=email.email_account_id if email else None, ip_address=ip)
    await session.commit()
    return draft


async def reject(session: AsyncSession, draft: EmailDraft, user_id: uuid.UUID, ip: str | None) -> EmailDraft:
    transition(draft, DraftStatus.REJECTED)
    draft.approved_at = None
    draft.approved_content_hash = None
    record_audit(session, action="draft.reject", resource_type="email_draft", resource_id=draft.id,
                 user_id=user_id, ip_address=ip)
    await session.commit()
    return draft


async def delete_draft(session: AsyncSession, draft: EmailDraft) -> None:
    if draft.status in (DraftStatus.SENDING, DraftStatus.SENT):
        raise InvalidStateTransition("Sent or sending drafts cannot be deleted")
    await session.delete(draft)
    await session.commit()


# --- Sending --------------------------------------------------------------------------------


async def send(
    session: AsyncSession, draft: EmailDraft, user_id: uuid.UUID, *, idempotency_key: str, ip: str | None
) -> tuple[EmailDraft, bool]:
    """Send an approved draft. Returns (draft, replayed) where replayed=True for an idempotent repeat."""
    if draft.idempotency_key == idempotency_key:
        if draft.status == DraftStatus.SENT:
            return draft, True
        if draft.status == DraftStatus.SENDING:
            raise ConflictError("This send request is already in progress", code="send_in_progress")
        raise ConflictError("This send request failed; approve the draft again and use a new key",
                            code="idempotency_key_used")
    if draft.status == DraftStatus.SENT:
        raise ConflictError("This draft has already been sent", code="already_sent")
    if draft.status != DraftStatus.APPROVED:
        raise InvalidStateTransition("Only approved drafts can be sent", details={"status": draft.status.value})
    if draft.approved_content_hash != content_hash(draft):
        raise ConflictError("Draft changed after approval; approve it again", code="approval_stale")

    email = await session.get(Email, draft.email_id)
    if email is None or email.user_id != user_id:
        raise NotFoundError("Draft not found")
    account = await session.get(EmailAccount, email.email_account_id)
    if account is None or account.user_id != user_id:
        raise NotFoundError("Draft not found")
    account_service.ensure_active(account)
    if not account_service.can_send(account):
        raise PermissionDeniedError("The connected account did not grant permission to send email; reconnect it",
                                    code="insufficient_scope")
    if reply_recipient(email, account) != draft.to_address:
        raise ConflictError("Recipient no longer matches the original email", code="recipient_mismatch")

    # Compare-and-set: only one request can move this draft from approved to sending.
    try:
        result = await session.execute(
            update(EmailDraft)
            .where(EmailDraft.id == draft.id, EmailDraft.status == DraftStatus.APPROVED,
                   EmailDraft.approved_content_hash == draft.approved_content_hash)
            .values(status=DraftStatus.SENDING, idempotency_key=idempotency_key, updated_at=utcnow())
        )
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("Idempotency key already used for another request", code="idempotency_key_used") from exc
    if result.rowcount != 1:
        await session.rollback()
        raise ConflictError("Draft is no longer approved (concurrent change)", code="concurrent_modification")
    await session.commit()
    await session.refresh(draft)

    client = account_service.provider_for(account)
    reply = OutgoingReply(
        to=draft.to_address, subject=draft.draft_subject, body=draft.draft_body,
        from_address=account.email_address, thread_id=email.provider_thread_id,
        in_reply_to=email.internet_message_id, references=email.references,
        original_provider_message_id=email.provider_message_id,
    )
    try:
        sent_id = await account_service.with_token(session, account, lambda t: client.send_reply(t, reply))
    except (IntegrationError, ValueError) as exc:
        code = getattr(exc, "code", "send_failed")
        if isinstance(exc, ProviderRequestError) and exc.status_code is None:
            code = SEND_OUTCOME_UNKNOWN
        elif isinstance(exc, InsufficientScopeError):
            code = "insufficient_scope"
        elif isinstance(exc, CredentialsRevokedError):
            code = "credentials_revoked"
        transition(draft, DraftStatus.FAILED)
        draft.error_code = code
        draft.error_message = str(exc)[:500]
        record_audit(session, action="draft.send", resource_type="email_draft", resource_id=draft.id,
                     user_id=user_id, email_account_id=account.id, outcome=AuditOutcome.FAILURE, ip_address=ip,
                     details={"error_code": code})
        await session.commit()
        metrics.DRAFTS_SENT.labels(outcome="failed").inc()
        raise SendFailedError(details={"error_code": code}) from exc

    transition(draft, DraftStatus.SENT)
    draft.sent_at = utcnow()
    draft.provider_sent_message_id = sent_id
    record_audit(session, action="draft.send", resource_type="email_draft", resource_id=draft.id,
                 user_id=user_id, email_account_id=account.id, ip_address=ip,
                 details={"provider": account.provider.value})
    await session.commit()
    metrics.DRAFTS_SENT.labels(outcome="sent").inc()
    return draft, False
