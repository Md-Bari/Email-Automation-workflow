from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import CurrentUser, DbSession, Pagination
from app.api.v1.endpoints.accounts import task_accepted
from app.core.exceptions import ConflictError, NotFoundError
from app.core.rate_limit import rate_limit
from app.db.models import Email
from app.db.models.enums import Priority, ProcessingStatus, TaskType
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.emails import (
    AnalysisOut,
    AnalyzeRequest,
    EmailDetail,
    EmailListItem,
    GenerateReplyRequest,
    MarkReadRequest,
)
from app.schemas.tasks import TaskAccepted
from app.services import account_service, draft_service, email_service, notification_service, task_service
from app.services.email_service import EmailFilters

router = APIRouter(prefix="/emails", tags=["emails"], responses=ERROR_RESPONSES)


def _list_item(email: Email) -> EmailListItem:
    item = EmailListItem.model_validate(email)
    item.has_attachments = bool(email.attachment_metadata)
    return item


def _detail(email: Email) -> EmailDetail:
    item = EmailDetail.model_validate(email)
    item.has_attachments = bool(email.attachment_metadata)
    return item


@router.get("", response_model=Page[EmailListItem])
async def list_emails(
    user: CurrentUser,
    session: DbSession,
    pagination: Pagination,
    account_id: uuid.UUID | None = None,
    priority: Priority | None = None,
    is_emergency: bool | None = None,
    needs_review: bool | None = None,
    processing_status: ProcessingStatus | None = None,
    is_read: bool | None = None,
    sender: Annotated[str | None, Query(max_length=320)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Search in subject")] = None,
    received_after: datetime | None = None,
    received_before: datetime | None = None,
) -> Page[EmailListItem]:
    filters = EmailFilters(
        account_id=account_id, priority=priority, is_emergency=is_emergency, needs_review=needs_review,
        processing_status=processing_status, is_read=is_read, sender=sender, q=q,
        received_after=received_after, received_before=received_before,
    )
    rows, total = await email_service.list_emails(session, user.id, filters, offset=pagination.offset,
                                                  limit=pagination.page_size)
    return Page[EmailListItem].build([_list_item(e) for e in rows], total, pagination.page, pagination.page_size)


@router.get("/{email_id}", response_model=EmailDetail)
async def get_email(email_id: uuid.UUID, user: CurrentUser, session: DbSession) -> EmailDetail:
    return _detail(await email_service.get_owned_email(session, email_id, user.id))


@router.get("/{email_id}/analysis", response_model=AnalysisOut)
async def get_analysis(email_id: uuid.UUID, user: CurrentUser, session: DbSession) -> AnalysisOut:
    email = await email_service.get_owned_email(session, email_id, user.id)
    if email.analysis is None:
        raise NotFoundError("This email has not been analyzed yet", code="analysis_not_found",
                            details={"processing_status": email.processing_status.value})
    return AnalysisOut.model_validate(email.analysis)


@router.post("/{email_id}/analyze", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("analyze", 60, 600))])
async def analyze(email_id: uuid.UUID, body: AnalyzeRequest, user: CurrentUser, session: DbSession) -> TaskAccepted:
    email = await email_service.get_owned_email(session, email_id, user.id)
    if await task_service.find_active(session, (TaskType.ANALYZE_EMAIL,), email.id):
        raise ConflictError("Analysis already in progress", code="analysis_in_progress")
    task = await task_service.create_and_dispatch(
        session, task_type=TaskType.ANALYZE_EMAIL, user_id=user.id, resource_id=email.id,
        email_account_id=email.email_account_id, payload={"email_id": str(email.id), "force": body.force},
    )
    return task_accepted(task)


@router.post("/{email_id}/generate-reply", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("generate-reply", 60, 600))])
async def generate_reply(
    email_id: uuid.UUID, body: GenerateReplyRequest, user: CurrentUser, session: DbSession
) -> TaskAccepted:
    """Queue generation of a new reply draft. The draft always requires review and approval."""
    email = await email_service.get_owned_email(session, email_id, user.id)
    account = await account_service.get_owned_account(session, email.email_account_id, user.id)
    draft_service.reply_recipient(email, account)  # fail fast for no-reply senders
    if await task_service.find_active(session, (TaskType.GENERATE_DRAFT,), email.id):
        raise ConflictError("A reply is already being generated", code="generation_in_progress")
    prefs = await notification_service.effective_preferences(session, user.id, email.email_account_id)
    tone = body.tone or prefs.default_reply_tone
    task = await task_service.create_and_dispatch(
        session, task_type=TaskType.GENERATE_DRAFT, user_id=user.id, resource_id=email.id,
        email_account_id=email.email_account_id,
        payload={"email_id": str(email.id), "tone": tone.value, "instructions": body.instructions},
    )
    return task_accepted(task)


@router.get("/{email_id}/thread", response_model=list[EmailDetail])
async def get_thread(email_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[EmailDetail]:
    email = await email_service.get_owned_email(session, email_id, user.id)
    return [_detail(e) for e in await email_service.get_thread(session, email)]


@router.post("/{email_id}/mark-read", response_model=EmailListItem)
async def mark_read(email_id: uuid.UUID, body: MarkReadRequest, user: CurrentUser, session: DbSession) -> EmailListItem:
    """Updates the in-app read state (the provider mailbox is not modified; read-only scope)."""
    email = await email_service.get_owned_email(session, email_id, user.id)
    return _list_item(await email_service.set_read(session, email, body.is_read))
