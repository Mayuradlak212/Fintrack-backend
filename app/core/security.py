import base64
import hashlib

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from flask_jwt_extended import create_access_token, create_refresh_token

from app.core.config import settings


def hash_password(plain: str) -> str:
    """Returns bcrypt hash of the plain-text password."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Returns True if plain matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Symmetric encryption (TOTP secrets at rest) ───────────────────────────────

def _fernet() -> Fernet:
    """
    Fernet key derived from SECRET_KEY. TOTP secrets are stored encrypted so a
    database dump alone does not hand over every user's second factor.

    NOTE: rotating SECRET_KEY makes existing TOTP secrets undecryptable — those
    users must re-enrol their authenticator app.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    """Returns None if the ciphertext cannot be decrypted (e.g. key rotated)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


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
