from __future__ import annotations

from app.core.database import db
from app.core.security import hash_password, verify_password, create_tokens
from app.models.user import User
from app.schemas.user import RegisterRequest, LoginRequest


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
    def update_user(user_id: str, data: dict) -> User:
        user = db.session.get(User, user_id)
        if not user:
            raise ValueError("User not found")
        
        for key, value in data.items():
            if value is not None:
                setattr(user, key, value)
        
        db.session.commit()
        return user
