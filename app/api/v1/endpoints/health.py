from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.dependencies import DbSession, KVStore
from app.schemas.dashboard import HealthOut, ReadinessOut

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthOut)
async def liveness() -> HealthOut:
    return HealthOut(status="ok")


@router.get("/ready", response_model=ReadinessOut, responses={503: {"model": ReadinessOut}})
async def readiness(session: DbSession, store: KVStore) -> JSONResponse:
    """Checks dependencies. Reports only booleans - no versions, hosts or error details."""
    checks = {"database": False, "redis": False}
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3)
        checks["database"] = True
    except Exception:  # noqa: S110 - readiness reports failure without details
        pass
    try:
        checks["redis"] = await asyncio.wait_for(store.ping(), timeout=3)
    except Exception:  # noqa: S110
        pass
    ok = all(checks.values())
    body = ReadinessOut(status="ready" if ok else "unavailable", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if ok else 503)
