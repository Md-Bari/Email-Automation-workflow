"""Nodes after analysis: notification creation/dispatch, reply draft generation, finalization.

None of these perform consequential external actions on the user's behalf: notifications go to
the user themself, and drafts always wait for explicit human approval.
"""

from __future__ import annotations

import uuid

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.agents.nodes.deps import deps
from app.agents.state import AnalysisState
from app.core.exceptions import AppError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Email, EmailAccount, EmailDraft
from app.db.models.enums import DraftStatus, NotificationType
from app.services import draft_service, notification_service
from app.services.dispatcher import DELIVER_NOTIFICATION

logger = get_logger(__name__)


async def create_notification(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    d = deps(config)
    email = await d.session.get(Email, uuid.UUID(state["email_id"]))
    if email is None or email.analysis is None:
        return {"notification_id": None}
    kind = NotificationType.EMERGENCY if state["decision"]["is_emergency"] else NotificationType.NEEDS_REVIEW
    notification = await notification_service.create_email_notification(d.session, email, email.analysis, kind)
    if notification is None:
        await d.session.commit()
        return {"notification_id": None, "delivery_ids": [], "trace": ["create_notification:deduplicated"]}
    delivery_ids = await notification_service.pending_delivery_ids(d.session, notification.id)
    await d.session.commit()
    return {
        "notification_id": str(notification.id),
        "delivery_ids": [str(i) for i in delivery_ids],
        "trace": [f"create_notification:{kind.value}"],
    }


async def dispatch_notifications(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    """Queue external deliveries. A broker/channel failure must never fail email processing."""
    d = deps(config)
    for delivery_id in state.get("delivery_ids", []):
        try:
            d.dispatcher.send(DELIVER_NOTIFICATION, {"delivery_id": delivery_id})
        except Exception as exc:
            logger.warning("delivery_dispatch_failed", delivery_id=delivery_id, error=type(exc).__name__)
    return {"trace": ["dispatch_notifications"]}


async def generate_reply_draft(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    d = deps(config)
    email = await d.session.get(Email, uuid.UUID(state["email_id"]))
    if email is None or email.analysis is None:
        return {"draft_id": None}
    prefs = await notification_service.effective_preferences(d.session, email.user_id, email.email_account_id)
    if not prefs.auto_generate_drafts:
        return {"draft_id": None, "trace": ["generate_reply_draft:disabled"]}
    existing = (
        await d.session.execute(
            select(EmailDraft.id).where(
                EmailDraft.email_id == email.id,
                EmailDraft.status.in_([DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED, DraftStatus.SENDING,
                                       DraftStatus.SENT]),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return {"draft_id": str(existing), "trace": ["generate_reply_draft:exists"]}
    account = await d.session.get(EmailAccount, email.email_account_id)
    try:
        reply = await draft_service.generate_reply(
            d.session, d.llm, email, tone=prefs.default_reply_tone, summary=email.analysis.summary
        )
        draft = await draft_service.create_draft_from_reply(
            d.session, email, account, reply, tone=prefs.default_reply_tone, model=d.llm.model_name
        )
        await d.session.commit()
    except (IntegrationError, AppError) as exc:
        # Analysis is already persisted; a draft can be requested again via the API.
        await d.session.rollback()
        logger.warning("draft_generation_failed", email_id=str(email.id), error=getattr(exc, "code", "error"))
        return {"draft_id": None, "draft_error": getattr(exc, "code", "draft_failed"),
                "trace": ["generate_reply_draft:failed"]}
    return {"draft_id": str(draft.id), "trace": ["generate_reply_draft"]}


async def finalize(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    status = "skipped" if state.get("skip_reason") else "analyzed"
    return {"status": status, "trace": ["finalize"]}
