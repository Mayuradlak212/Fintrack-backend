from __future__ import annotations

from datetime import timedelta
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings — loaded from environment variables / .env file.
    All fields are type-safe via Pydantic v2.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    FLASK_ENV: str = "development"
    FLASK_DEBUG: bool = True
    SECRET_KEY: str = "dev-secret-change-in-prod"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/finance_tracker"

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "dev-jwt-secret-change-in-prod"
    JWT_ACCESS_TOKEN_EXPIRES_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRES_DAYS: int = 30

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # ── SMTP / Email ──────────────────────────────────────────────────────────
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@financetracker.com"

    # ── App URL (used in password-reset links) ────────────────────────────────
    FRONTEND_URL: str = "http://localhost:3000"

    # ── Password Reset ────────────────────────────────────────────────────────
    PASSWORD_RESET_TOKEN_EXPIRES_MINUTES: int = 30

    # ── TOTP / Two-Factor Auth ────────────────────────────────────────────────
    TOTP_ISSUER: str = "FinTrack"
    # Window of the pending "password OK, code still needed" login challenge.
    MFA_CHALLENGE_EXPIRES_MINUTES: int = 5
    # Wrong codes allowed per challenge before it is burned. A 6-digit code has
    # only 1M combinations, so an unthrottled endpoint is brute-forceable.
    MFA_MAX_ATTEMPTS: int = 5
    BACKUP_CODE_COUNT: int = 10

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    # Optional. Without it, limits are per-process: with N gunicorn workers the
    # effective limit is up to N times the configured one. Set it in production.
    REDIS_URL: str = ""
    # Kept tight on purpose — a slow shared store must never become latency on
    # every request. On timeout the limiter falls back to per-process limits.
    RATE_LIMIT_REDIS_TIMEOUT_MS: int = 50
    # Only trust X-Forwarded-For when actually behind a proxy that rewrites it
    # (Vercel, nginx). Trusting it on a directly-exposed server lets any caller
    # pick their own rate-limit bucket.
    RATE_LIMIT_TRUST_PROXY: bool = True

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    @property
    def access_token_expires(self) -> timedelta:
        return timedelta(minutes=self.JWT_ACCESS_TOKEN_EXPIRES_MINUTES)

    @property
    def refresh_token_expires(self) -> timedelta:
        return timedelta(days=self.JWT_REFRESH_TOKEN_EXPIRES_DAYS)


# Singleton — import this everywhere
settings = Settings()
