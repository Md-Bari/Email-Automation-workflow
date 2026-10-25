from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import CurrentUser, DbSession, Pagination
from app.api.v1.endpoints.accounts import task_accepted
from app.core.rate_limit import rate_limit
from app.db.models.enums import TaskStatus, TaskType
from app.schemas.common import ERROR_RESPONSES, ErrorResponse, Page
from app.schemas.tasks import TaskAccepted, TaskOut
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"], responses=ERROR_RESPONSES)


@router.get("", response_model=Page[TaskOut])
async def list_tasks(
    user: CurrentUser, session: DbSession, pagination: Pagination,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None, task_type: TaskType | None = None,
) -> Page[TaskOut]:
    rows, total = await task_service.list_tasks(session, user.id, status=status_filter, task_type=task_type,
                                                offset=pagination.offset, limit=pagination.page_size)
    return Page[TaskOut].build([TaskOut.model_validate(t) for t in rows], total, pagination.page,
                               pagination.page_size)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, user: CurrentUser, session: DbSession) -> TaskOut:
    return TaskOut.model_validate(await task_service.get_owned_task(session, task_id, user.id))


@router.post("/{task_id}/retry", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("task-retry", 30, 600))], responses={409: {"model": ErrorResponse}})
async def retry_task(task_id: uuid.UUID, user: CurrentUser, session: DbSession) -> TaskAccepted:
    """Retry a failed task (owner only, eligible task types, bounded by TASK_MANUAL_RETRY_LIMIT)."""
    return task_accepted(await task_service.retry_task(session, task_id, user.id))
