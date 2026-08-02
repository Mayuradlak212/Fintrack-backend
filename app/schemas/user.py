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
    totp_enabled: bool = False
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


# ── TOTP two-factor auth ───────────────────────────────────────────────────────

class TotpCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class TotpDisableRequest(BaseModel):
    password: str = Field(min_length=1)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=1)
    code: str = Field(min_length=6, max_length=32)


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_base64: str


class TotpActivateResponse(BaseModel):
    message: str = "Two-factor authentication enabled."
    backup_codes: list[str]


class MfaRequiredResponse(BaseModel):
    """Returned by /login when the password is correct but a code is still needed."""
    mfa_required: bool = True
    mfa_token: str
    message: str = "Enter the code from your authenticator app."
