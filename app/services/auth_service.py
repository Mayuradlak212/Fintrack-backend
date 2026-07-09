from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.core.database import db
from app.core.security import hash_password, verify_password, create_tokens
from app.models.user import User
from app.schemas.user import RegisterRequest, LoginRequest
from app.core.config import settings


class AuthService:
    """Handles user registration, login, and token refresh."""

    @staticmethod
    def register(data: RegisterRequest) -> dict:
        """
        Registers a new user.
        Raises ValueError if email is already taken.
        """
        if User.query.filter_by(email=data.email).first():
            raise ValueError(f"Email '{data.email}' is already registered.")

        user = User(
            email=data.email,
            name=data.name,
            password_hash=hash_password(data.password),
        )
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)

        tokens = create_tokens(user.id)
        return {"user": user, **tokens}

    @staticmethod
    def login(data: LoginRequest) -> dict:
        """
        Authenticates user credentials.
        Raises ValueError on bad email or password.
        """
        user = User.query.filter_by(email=data.email).first()
        if not user or not verify_password(data.password, user.password_hash):
            raise ValueError("Invalid email or password.")

        tokens = create_tokens(user.id)
        return {"user": user, **tokens}

    @staticmethod
    def get_by_id(user_id: str) -> User | None:
        return db.session.get(User, user_id)

    @staticmethod
    def get_by_id_email(email: str) -> User | None:
        return User.query.filter_by(email=email.strip().lower()).first()

    @staticmethod
    def update_user(user_id: str, data: dict) -> User:
        user = db.session.get(User, user_id)
        if not user:
            raise ValueError("User not found")
        
        for key, value in data.items():
            if value is not None:
                setattr(user, key, value)
        
        db.session.commit()
        return user

    # ── Password Reset ────────────────────────────────────────────────────────

    @staticmethod
    def forgot_password(email: str) -> str | None:
        """
        Generate a password-reset token for the given email.
        Returns the raw token (to be embedded in the reset URL).
        Returns None silently if email not found (prevents user enumeration).
        """
        user = User.query.filter_by(email=email).first()
        if not user:
            return None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        user.password_reset_token = token_hash
        user.password_reset_expires = datetime.now(timezone.utc) + timedelta(
            minutes=settings.PASSWORD_RESET_TOKEN_EXPIRES_MINUTES
        )
        db.session.commit()
        return raw_token

    @staticmethod
    def reset_password(token: str, new_password: str) -> bool:
        """
        Validate the reset token and update the user's password.
        Returns True on success, False on invalid/expired token.
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = User.query.filter_by(password_reset_token=token_hash).first()

        if not user:
            return False
        if not user.password_reset_expires:
            return False
        if datetime.now(timezone.utc) > user.password_reset_expires.replace(tzinfo=timezone.utc):
            return False

        user.password_hash = hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires = None
        db.session.commit()
        return True
