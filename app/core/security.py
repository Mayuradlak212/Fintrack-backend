import bcrypt
from flask_jwt_extended import create_access_token, create_refresh_token

from app.core.config import settings


def hash_password(plain: str) -> str:
    """Returns bcrypt hash of the plain-text password."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Returns True if plain matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_tokens(user_id: str) -> dict[str, str]:
    """Creates a pair of access + refresh JWT tokens for the given user id."""
    return {
        "access_token": create_access_token(
            identity=user_id,
            expires_delta=settings.access_token_expires,
        ),
        "refresh_token": create_refresh_token(
            identity=user_id,
            expires_delta=settings.refresh_token_expires,
        ),
    }
