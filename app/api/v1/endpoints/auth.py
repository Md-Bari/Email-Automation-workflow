from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import ClientIP, CurrentUser, DbSession, KVStore
from app.core.rate_limit import rate_limit
from app.schemas.auth import (
    DeleteAccountRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.schemas.common import ERROR_RESPONSES
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(rate_limit("register", 10, 3600))])
async def register(body: RegisterRequest, session: DbSession) -> UserOut:
    user = await auth_service.register(session, body.email, body.password)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(rate_limit("login", 20, 300))])
async def login(body: LoginRequest, session: DbSession, store: KVStore, ip: ClientIP) -> TokenResponse:
    tokens = await auth_service.login(session, store, body.email, body.password, ip)
    return TokenResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                         expires_in=tokens.expires_in)


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(rate_limit("refresh", 60, 300))])
async def refresh(body: RefreshRequest, session: DbSession) -> TokenResponse:
    tokens = await auth_service.refresh(session, body.refresh_token)
    return TokenResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                         expires_in=tokens.expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, user: CurrentUser, session: DbSession) -> Response:
    await auth_service.logout(session, user.id, body.refresh_token, body.everywhere)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(rate_limit("delete-user", 5, 3600))])
async def delete_me(body: DeleteAccountRequest, user: CurrentUser, session: DbSession) -> Response:
    """Permanently delete your user, connected accounts, emails, analyses, drafts and notifications."""
    await auth_service.delete_user(session, user, body.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
