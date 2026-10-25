from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.kvstore import KeyValueStore, get_kv_store
from app.core.rate_limit import client_ip
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db
from app.schemas.common import PageParams

bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]
KVStore = Annotated[KeyValueStore, Depends(get_kv_store)]


async def get_current_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AuthenticationError("Invalid access token", code="invalid_token") from exc
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User not found or inactive", code="invalid_token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def page_params(
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


Pagination = Annotated[PageParams, Depends(page_params)]


def get_client_ip(request: Request) -> str:
    return client_ip(request)


ClientIP = Annotated[str, Depends(get_client_ip)]
