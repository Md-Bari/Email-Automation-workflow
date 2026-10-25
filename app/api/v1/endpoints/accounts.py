from __future__ import annotations

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.api.dependencies import ClientIP, CurrentUser, DbSession, KVStore
from app.core.config import get_settings
from app.core.exceptions import ConflictError, IntegrationError, ValidationFailed
from app.core.logging import get_logger
from app.core.rate_limit import rate_limit
from app.db.models import BackgroundTask, EmailAccount
from app.db.models.enums import EmailProvider, TaskType
from app.schemas.accounts import AccountOut, ConnectResponse, OAuthResult, SyncStatusOut
from app.schemas.common import ERROR_RESPONSES
from app.schemas.tasks import TaskAccepted, TaskOut
from app.services import account_service, task_service

router = APIRouter(prefix="/accounts", tags=["accounts"], responses=ERROR_RESPONSES)
logger = get_logger(__name__)
OAUTH_PROVIDERS = {"gmail": EmailProvider.GMAIL, "microsoft": EmailProvider.MICROSOFT}


def account_out(account: EmailAccount) -> AccountOut:
    out = AccountOut.model_validate(account)
    try:
        out.can_send = account_service.can_send(account)
    except ValidationFailed:
        out.can_send = False  # provider disabled in this deployment
    return out


def task_accepted(task: BackgroundTask) -> TaskAccepted:
    prefix = get_settings().api_v1_prefix
    return TaskAccepted(task_id=task.id, status=task.status, task_type=task.task_type,
                        status_url=f"{prefix}/tasks/{task.id}")


def _provider(name: str) -> EmailProvider:
    if name not in OAUTH_PROVIDERS:
        raise ValidationFailed(f"Unsupported provider '{name}'", code="unsupported_provider")
    return OAUTH_PROVIDERS[name]


@router.get("", response_model=list[AccountOut])
async def list_accounts(user: CurrentUser, session: DbSession) -> list[AccountOut]:
    return [account_out(a) for a in await account_service.list_accounts(session, user.id)]


@router.post("/sandbox/connect", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def connect_sandbox(user: CurrentUser, session: DbSession) -> AccountOut:
    """Connect a mock mailbox with sample emails (only when SANDBOX_MODE=true). Sends are never delivered."""
    return account_out(await account_service.connect_sandbox(session, user))


@router.post("/{provider}/connect", response_model=ConnectResponse,
             dependencies=[Depends(rate_limit("oauth-connect", 20, 600))])
async def connect(provider: str, user: CurrentUser, store: KVStore) -> ConnectResponse:
    """Start OAuth. Open ``authorization_url`` in the browser; the provider redirects to the callback."""
    url = await account_service.start_oauth(store, user, _provider(provider))
    return ConnectResponse(authorization_url=url, expires_in=get_settings().oauth_state_ttl_seconds)


@router.get("/{provider}/callback", response_model=OAuthResult,
            dependencies=[Depends(rate_limit("oauth-callback", 30, 600))])
async def callback(
    provider: str,
    session: DbSession,
    store: KVStore,
    ip: ClientIP,
    state: str | None = Query(default=None, max_length=200),
    code: str | None = Query(default=None, max_length=2000),
    error: str | None = Query(default=None, max_length=200),
) -> Response:
    """OAuth redirect target. Validates the single-use state and links the mailbox to the initiating user."""
    settings = get_settings()
    try:
        account = await account_service.complete_oauth(
            session, store, provider=_provider(provider), state=state, code=code, error=error, ip=ip
        )
        result = OAuthResult(status="connected", account_id=account.id)
    except account_service.OAuthCallbackError as exc:
        result = OAuthResult(status="error", error_code=exc.code, message=str(exc))
    except IntegrationError as exc:
        logger.warning("oauth_callback_provider_error", code=exc.code)
        result = OAuthResult(status="error", error_code=exc.code, message="The provider rejected the request")
    if settings.frontend_url:
        params = {k: str(v) for k, v in result.model_dump(exclude_none=True).items() if k != "message"}
        return RedirectResponse(f"{settings.frontend_url.rstrip('/')}/oauth/result?{urlencode(params)}",
                                status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(result.model_dump(mode="json"), status_code=200 if result.status == "connected" else 400)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(account_id: uuid.UUID, user: CurrentUser, session: DbSession) -> AccountOut:
    return account_out(await account_service.get_owned_account(session, account_id, user.id))


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(account_id: uuid.UUID, user: CurrentUser, session: DbSession, ip: ClientIP) -> Response:
    """Revoke provider credentials and delete the account with all synchronized data."""
    account = await account_service.get_owned_account(session, account_id, user.id)
    await account_service.disconnect(session, account, ip)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{account_id}/sync", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("manual-sync", 30, 600))])
async def sync(account_id: uuid.UUID, user: CurrentUser, session: DbSession) -> TaskAccepted:
    account = await account_service.get_owned_account(session, account_id, user.id)
    account_service.ensure_active(account)
    active = await task_service.find_active(session, (TaskType.INITIAL_SYNC, TaskType.INCREMENTAL_SYNC), account.id)
    if active:
        raise ConflictError("A synchronization is already queued or running", code="sync_in_progress",
                            details={"task_id": str(active.id)})
    task = await account_service.enqueue_sync(session, account)
    return task_accepted(task)


@router.get("/{account_id}/sync-status", response_model=SyncStatusOut)
async def sync_status(account_id: uuid.UUID, user: CurrentUser, session: DbSession) -> SyncStatusOut:
    account = await account_service.get_owned_account(session, account_id, user.id)
    sync_types = (TaskType.INITIAL_SYNC, TaskType.INCREMENTAL_SYNC)
    active = await task_service.find_active(session, sync_types, account.id)
    last = (
        await session.execute(
            select(BackgroundTask)
            .where(BackgroundTask.resource_id == account.id, BackgroundTask.task_type.in_(sync_types))
            .order_by(BackgroundTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return SyncStatusOut(
        account_id=account.id,
        connection_status=account.connection_status,
        last_synced_at=account.last_synced_at,
        incremental_sync_ready=account.synchronization_cursor is not None,
        active_task=TaskOut.model_validate(active) if active else None,
        last_task=TaskOut.model_validate(last) if last else None,
    )
