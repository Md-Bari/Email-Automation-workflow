from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128, description="At least 12 characters.")

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        if value.strip() != value or len(set(value)) < 6:
            raise ValueError("password is too weak")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=200)
    everywhere: bool = Field(False, description="Revoke all sessions of this user.")


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
