from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.dependencies import ClientIP, CurrentUser, DbSession, Pagination
from app.api.v1.endpoints.accounts import task_accepted
from app.core.exceptions import ConflictError, InvalidStateTransition
from app.core.rate_limit import rate_limit
from app.db.models.enums import DraftStatus, TaskType
from app.schemas.common import ERROR_RESPONSES, ErrorResponse, Page
from app.schemas.drafts import DraftOut, DraftUpdate, RegenerateRequest, SendRequest, SendResponse
from app.schemas.tasks import TaskAccepted
from app.services import draft_service, task_service

router = APIRouter(prefix="/drafts", tags=["drafts"], responses=ERROR_RESPONSES)

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_\-:.]+$",
           description="Unique key per send attempt (e.g. a UUID). Repeating it returns the original result."),
]


@router.get("", response_model=Page[DraftOut])
async def list_drafts(
    user: CurrentUser, session: DbSession, pagination: Pagination,
    status_filter: Annotated[DraftStatus | None, Query(alias="status")] = None,
    email_id: uuid.UUID | None = None,
) -> Page[DraftOut]:
    rows, total = await draft_service.list_drafts(session, user.id, status=status_filter, email_id=email_id,
                                                  offset=pagination.offset, limit=pagination.page_size)
    return Page[DraftOut].build([DraftOut.model_validate(d) for d in rows], total, pagination.page,
                                pagination.page_size)


@router.get("/{draft_id}", response_model=DraftOut)
async def get_draft(draft_id: uuid.UUID, user: CurrentUser, session: DbSession) -> DraftOut:
    return DraftOut.model_validate(await draft_service.get_owned_draft(session, draft_id, user.id))


@router.patch("/{draft_id}", response_model=DraftOut, responses={409: {"model": ErrorResponse}})
async def update_draft(draft_id: uuid.UUID, body: DraftUpdate, user: CurrentUser, session: DbSession) -> DraftOut:
    """Edit a draft. Editing an approved draft returns it to ``pending_review``."""
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    draft = await draft_service.update_draft(session, draft, body=body.draft_body, subject=body.draft_subject,
                                             tone=body.tone)
    return DraftOut.model_validate(draft)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT, responses={409: {"model": ErrorResponse}})
async def delete_draft(draft_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    await draft_service.delete_draft(session, draft)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{draft_id}/regenerate", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("regenerate", 60, 600))], responses={409: {"model": ErrorResponse}})
async def regenerate(draft_id: uuid.UUID, body: RegenerateRequest, user: CurrentUser, session: DbSession
                     ) -> TaskAccepted:
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    if draft.status not in draft_service.EDITABLE:
        raise InvalidStateTransition(f"Cannot regenerate a draft in state '{draft.status.value}'")
    if await task_service.find_active(session, (TaskType.GENERATE_DRAFT,), draft.email_id):
        raise ConflictError("A reply is already being generated", code="generation_in_progress")
    task = await task_service.create_and_dispatch(
        session, task_type=TaskType.GENERATE_DRAFT, user_id=user.id, resource_id=draft.email_id,
        payload={"email_id": str(draft.email_id), "draft_id": str(draft.id),
                 "tone": (body.tone or draft.tone).value, "instructions": body.instructions},
    )
    return task_accepted(task)


@router.post("/{draft_id}/approve", response_model=DraftOut, responses={409: {"model": ErrorResponse}})
async def approve(draft_id: uuid.UUID, user: CurrentUser, session: DbSession, ip: ClientIP) -> DraftOut:
    """Approve the current content. Approval does **not** send; use ``/send`` separately."""
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    return DraftOut.model_validate(await draft_service.approve(session, draft, user.id, ip))


@router.post("/{draft_id}/reject", response_model=DraftOut, responses={409: {"model": ErrorResponse}})
async def reject(draft_id: uuid.UUID, user: CurrentUser, session: DbSession, ip: ClientIP) -> DraftOut:
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    return DraftOut.model_validate(await draft_service.reject(session, draft, user.id, ip))


@router.post(
    "/{draft_id}/send", response_model=SendResponse,
    dependencies=[Depends(rate_limit("send", 30, 600))],
    responses={409: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def send(
    draft_id: uuid.UUID, body: SendRequest, idempotency_key: IdempotencyKey,
    user: CurrentUser, session: DbSession, ip: ClientIP,
) -> SendResponse:
    """Send an **approved** draft. Requires ``{"confirm": true}`` and an ``Idempotency-Key`` header."""
    draft = await draft_service.get_owned_draft(session, draft_id, user.id)
    draft, replayed = await draft_service.send(session, draft, user.id, idempotency_key=idempotency_key, ip=ip)
    return SendResponse(draft=DraftOut.model_validate(draft), replayed=replayed)
