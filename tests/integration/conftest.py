"""Fixtures for service-level tests: a user with a connected sandbox account."""

from __future__ import annotations

import pytest

from app.db.models import User
from app.services import account_service


@pytest.fixture
async def user(session) -> User:
    u = User(email="owner@example.com", hashed_password=None, is_active=True)
    session.add(u)
    await session.commit()
    return u


@pytest.fixture
async def sandbox_account(session, user, dispatcher):
    account = await account_service.connect_sandbox(session, user)
    return account
