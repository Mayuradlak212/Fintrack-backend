from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


# ── Request schemas ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class UserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    avatar_base64: str | None = None
    avatar_mime_type: str | None = None
    phone: str | None = Field(default=None, pattern=r"^\d{10}$")


# ── Response schemas ───────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    avatar_base64: str | None = None
    avatar_mime_type: str | None = None
    phone: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    user: UserResponse


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
