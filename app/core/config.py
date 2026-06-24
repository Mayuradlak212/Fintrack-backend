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
