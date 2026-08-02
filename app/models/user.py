from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import db


class User(db.Model):
    """Application user — stores credentials and profile."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    avatar_base64: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_mime_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Password-reset one-time token (URL-safe, expires)
    password_reset_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    password_reset_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── TOTP two-factor auth ──────────────────────────────────────────────────
    # Base32 TOTP secret, Fernet-encrypted (see core.security.encrypt_secret).
    # Present but with totp_enabled False means setup was started, never confirmed.
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # JSON array of sha256 hashes; each code is single-use and removed on use.
    totp_backup_codes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pending login challenge — password accepted, awaiting the 6-digit code.
    # Deliberately opaque and DB-backed rather than a JWT, so it can never be
    # mistaken for an access token by a protected endpoint.
    mfa_challenge_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    mfa_challenge_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Relationship back-reference (lazy loaded)
    transactions: Mapped[list["Transaction"]] = db.relationship(  # type: ignore[name-defined]
        "Transaction", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"
