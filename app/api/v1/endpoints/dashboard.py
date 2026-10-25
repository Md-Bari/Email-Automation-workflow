from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.common import ERROR_RESPONSES
from app.schemas.dashboard import ActivityItem, DashboardSummary
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"], responses=ERROR_RESPONSES)


@router.get("/summary", response_model=DashboardSummary)
async def summary(user: CurrentUser, session: DbSession) -> DashboardSummary:
    return DashboardSummary(**await dashboard_service.summary(session, user.id))


@router.get("/activity", response_model=list[ActivityItem])
async def activity(
    user: CurrentUser, session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ActivityItem]:
    return [ActivityItem.model_validate(a) for a in await dashboard_service.activity(session, user.id, limit)]
