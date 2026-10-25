"""Registration, login with lockout, refresh-token rotation with reuse detection, logout, deletion."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, ConflictError, RateLimitedError
from app.core.kvstore import KeyValueStore
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.db.base import utcnow
from app.db.models import RefreshToken, User
from app.db.models.enums import AuditOutcome
from app.services.audit_service import record_audit


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def register(session: AsyncSession, email: str, password: str) -> User:
    user = User(email=normalize_email(email), hashed_password=hash_password(password), is_active=True)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("An account with this email already exists", code="email_taken") from exc
    record_audit(session, action="user.register", resource_type="user", resource_id=user.id, user_id=user.id)
    await session.commit()
    return user


async def _issue_tokens(session: AsyncSession, user: User, family_id: uuid.UUID | None = None) -> TokenPair:
    settings = get_settings()
    raw = generate_opaque_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw),
            family_id=family_id or uuid.uuid4(),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    access, ttl = create_access_token(user.id)
    return TokenPair(access_token=access, refresh_token=raw, expires_in=ttl)


async def login(
    session: AsyncSession, store: KeyValueStore, email: str, password: str, ip: str | None
) -> TokenPair:
    settings = get_settings()
    email = normalize_email(email)
    lock_key = f"login-fail:{email}"
    failures = await store.get(lock_key)
    if failures and int(failures) >= settings.login_max_attempts:
        raise RateLimitedError(settings.login_lockout_seconds, "Too many failed login attempts; try again later")

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password) or not user.is_active:
        await store.incr(lock_key, settings.login_lockout_seconds)
        record_audit(session, action="user.login", resource_type="user", outcome=AuditOutcome.FAILURE,
                     user_id=user.id if user else None, ip_address=ip)
        await session.commit()
        # Same message for unknown email, wrong password and inactive user (no account enumeration).
        raise AuthenticationError("Invalid email or password", code="invalid_credentials")

    await store.delete(lock_key)
    if user.hashed_password and password_needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)
    tokens = await _issue_tokens(session, user)
    record_audit(session, action="user.login", resource_type="user", resource_id=user.id, user_id=user.id,
                 ip_address=ip)
    await session.commit()
    return tokens


async def refresh(session: AsyncSession, raw_refresh_token: str) -> TokenPair:
    now = utcnow()
    token = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_refresh_token)))
    ).scalar_one_or_none()
    if token is None:
        raise AuthenticationError("Invalid refresh token", code="invalid_refresh_token")
    if token.rotated_at is not None or token.revoked_at is not None:
        # A rotated token was presented again: assume theft and revoke the whole family.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == token.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        record_audit(session, action="auth.refresh_reuse_detected", resource_type="refresh_token",
                     outcome=AuditOutcome.DENIED, user_id=token.user_id)
        await session.commit()
        raise AuthenticationError("Refresh token is no longer valid", code="invalid_refresh_token")
    if token.expires_at <= now:
        raise AuthenticationError("Refresh token expired", code="refresh_token_expired")

    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Invalid refresh token", code="invalid_refresh_token")

    # Compare-and-set so two concurrent refreshes cannot both succeed with the same token.
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id == token.id, RefreshToken.rotated_at.is_(None), RefreshToken.revoked_at.is_(None))
        .values(rotated_at=now)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise AuthenticationError("Refresh token is no longer valid", code="invalid_refresh_token")
    tokens = await _issue_tokens(session, user, family_id=token.family_id)
    await session.commit()
    return tokens


async def logout(session: AsyncSession, user_id: uuid.UUID, raw_refresh_token: str | None, everywhere: bool) -> None:
    now = utcnow()
    query = update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
    if not everywhere:
        if not raw_refresh_token:
            return
        token = (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == hash_token(raw_refresh_token), RefreshToken.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if token is None:
            return
        query = query.where(RefreshToken.family_id == token.family_id)
    await session.execute(query.values(revoked_at=now))
    record_audit(session, action="user.logout", resource_type="user", resource_id=user_id, user_id=user_id,
                 details={"everywhere": everywhere})
    await session.commit()


async def delete_user(session: AsyncSession, user: User, password: str) -> None:
    if not verify_password(password, user.hashed_password):
        raise AuthenticationError("Password confirmation failed", code="invalid_credentials")
    record_audit(session, action="user.delete", resource_type="user", resource_id=user.id, user_id=None)
    await session.delete(user)
    await session.commit()
