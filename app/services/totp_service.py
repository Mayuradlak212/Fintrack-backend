from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode

from app.core.config import settings
from app.core.database import db
from app.core.security import decrypt_secret, encrypt_secret, verify_password
from app.models.user import User


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Postgres may hand back naive datetimes depending on the driver."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TotpService:
    """TOTP enrolment, verification, backup codes, and the login MFA challenge."""

    # ── Enrolment ─────────────────────────────────────────────────────────────

    @staticmethod
    def begin_setup(user: User) -> dict:
        """
        Generate (or regenerate) an unconfirmed TOTP secret and return the data
        needed to display a QR code. Does NOT enable 2FA — activate() does that
        once the user proves their app is working.
        """
        if user.totp_enabled:
            raise ValueError("Two-factor authentication is already enabled.")

        secret = pyotp.random_base32()
        user.totp_secret = encrypt_secret(secret)
        user.totp_confirmed_at = None
        db.session.commit()

        uri = pyotp.TOTP(secret).provisioning_uri(
            name=user.email,
            issuer_name=settings.TOTP_ISSUER,
        )

        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        return {
            "secret": secret,  # shown once, for manual entry
            "otpauth_uri": uri,
            "qr_base64": base64.b64encode(buf.getvalue()).decode(),
        }

    @staticmethod
    def activate(user: User, code: str) -> list[str]:
        """
        Confirm setup with a code from the authenticator app. Enables 2FA and
        returns freshly generated backup codes (the only time they are visible).
        """
        if user.totp_enabled:
            raise ValueError("Two-factor authentication is already enabled.")
        if not user.totp_secret:
            raise ValueError("Start setup first.")

        secret = decrypt_secret(user.totp_secret)
        if not secret or not TotpService._check(secret, code):
            raise ValueError("That code is not valid. Check your authenticator app and try again.")

        plain_codes = [
            f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"
            for _ in range(settings.BACKUP_CODE_COUNT)
        ]

        user.totp_enabled = True
        user.totp_confirmed_at = _now()
        user.totp_backup_codes = json.dumps([_hash(c) for c in plain_codes])
        db.session.commit()

        return plain_codes

    @staticmethod
    def disable(user: User, password: str) -> None:
        """Turn 2FA off. Requires the account password, not just a session."""
        if not verify_password(password, user.password_hash):
            raise ValueError("Incorrect password.")

        user.totp_secret = None
        user.totp_enabled = False
        user.totp_confirmed_at = None
        user.totp_backup_codes = None
        TotpService._clear_challenge(user)
        db.session.commit()

    # ── Verification ──────────────────────────────────────────────────────────

    @staticmethod
    def _check(secret: str, code: str) -> bool:
        # valid_window=1 accepts the adjacent 30s steps, covering clock drift
        # between the user's phone and the server.
        return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)

    @staticmethod
    def _consume_backup_code(user: User, code: str) -> bool:
        """Single-use: a matching code is removed so it cannot be replayed."""
        if not user.totp_backup_codes:
            return False
        try:
            hashes: list[str] = json.loads(user.totp_backup_codes)
        except (ValueError, TypeError):
            return False

        candidate = _hash(code.lower())
        if candidate not in hashes:
            return False

        hashes.remove(candidate)
        user.totp_backup_codes = json.dumps(hashes)
        return True

    # ── Login challenge ───────────────────────────────────────────────────────

    @staticmethod
    def start_challenge(user: User) -> str:
        """Issue the short-lived token that stands in for a half-finished login."""
        raw = secrets.token_urlsafe(32)
        user.mfa_challenge_token = _hash(raw)
        user.mfa_challenge_expires = _now() + timedelta(
            minutes=settings.MFA_CHALLENGE_EXPIRES_MINUTES
        )
        user.mfa_attempts = 0
        db.session.commit()
        return raw

    @staticmethod
    def _clear_challenge(user: User) -> None:
        user.mfa_challenge_token = None
        user.mfa_challenge_expires = None
        user.mfa_attempts = 0

    @staticmethod
    def verify_challenge(mfa_token: str, code: str) -> User:
        """
        Complete a pending login. Accepts either a TOTP code or a backup code.
        Raises ValueError on anything invalid — the message is intentionally
        vague about which part failed.
        """
        user = User.query.filter_by(mfa_challenge_token=_hash(mfa_token)).first()
        if not user:
            raise ValueError("This login attempt has expired. Please sign in again.")

        expires = _aware(user.mfa_challenge_expires)
        if not expires or _now() > expires:
            TotpService._clear_challenge(user)
            db.session.commit()
            raise ValueError("This login attempt has expired. Please sign in again.")

        if user.mfa_attempts >= settings.MFA_MAX_ATTEMPTS:
            TotpService._clear_challenge(user)
            db.session.commit()
            raise ValueError("Too many incorrect codes. Please sign in again.")

        secret = decrypt_secret(user.totp_secret) if user.totp_secret else None
        ok = bool(secret and TotpService._check(secret, code))
        if not ok:
            ok = TotpService._consume_backup_code(user, code)

        if not ok:
            user.mfa_attempts += 1
            remaining = settings.MFA_MAX_ATTEMPTS - user.mfa_attempts
            db.session.commit()
            if remaining <= 0:
                raise ValueError("Too many incorrect codes. Please sign in again.")
            raise ValueError(f"Incorrect code. {remaining} attempt(s) remaining.")

        TotpService._clear_challenge(user)
        db.session.commit()
        return user
