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

    # ── Redis high availability ───────────────────────────────────────────────
    # Comma-separated sentinel endpoints, e.g.
    #   "localhost:26379,localhost:26380,localhost:26381".
    # When set, the app resolves the current primary through Sentinel instead of
    # pinning REDIS_URL to a fixed host, so a failover does not need a redeploy.
    # REDIS_URL still works on its own for single-node / managed Redis.
    REDIS_SENTINELS: str = ""
    # Must match `sentinel monitor <name>` in sentinel.conf.
    REDIS_MASTER_NAME: str = "fintrack-primary"
    REDIS_PASSWORD: str = ""
    # Sentinels usually carry their own auth, separate from the data nodes.
    REDIS_SENTINEL_PASSWORD: str = ""
    REDIS_DB: int = 0
    # Data-node socket timeout. Same reasoning as the limiter's: a slow Redis
    # must not become latency on every request.
    REDIS_SOCKET_TIMEOUT_MS: int = 200
    # Sentinel lookups are off the hot path (only on connect / after failover),
    # so they can afford to be more patient than a data call.
    REDIS_SENTINEL_TIMEOUT_MS: int = 500

    # ── Replication safety ────────────────────────────────────────────────────
    # How many replicas must acknowledge a *critical* write before the app
    # treats it as durable. WAIT blocks for at most REDIS_WAIT_TIMEOUT_MS and
    # reports how many actually acked; 0 disables the check.
    # This is a safety net, not a transaction: WAIT reduces the window in which
    # an acknowledged write is lost to a failover, it does not close it.
    REDIS_WAIT_REPLICAS: int = 1
    REDIS_WAIT_TIMEOUT_MS: int = 200
    # When fewer than REDIS_WAIT_REPLICAS acked, raise instead of returning
    # quietly. Off by default so existing call sites keep their behaviour;
    # critical_write() callers opt in per call.
    REDIS_WAIT_RAISE_ON_SHORTFALL: bool = False

    # ── Redis health reporting ────────────────────────────────────────────────
    REDIS_HEALTH_TIMEOUT_MS: int = 1000
    # Replica lag above this many seconds is reported as degraded. Mirror the
    # primary's `min-replicas-max-lag` so the health endpoint warns before the
    # primary starts rejecting writes.
    REDIS_MAX_REPLICA_LAG_SECONDS: int = 10

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

    @property
    def sentinel_endpoints(self) -> List[tuple]:
        """[(host, port), ...] parsed from REDIS_SENTINELS. Empty when unset."""
        endpoints: List[tuple] = []
        for raw in self.REDIS_SENTINELS.split(","):
            entry = raw.strip()
            if not entry:
                continue
            host, _, port = entry.rpartition(":")
            if not host:
                raise ValueError(
                    f"REDIS_SENTINELS entry {entry!r} must be in host:port form"
                )
            endpoints.append((host.strip(), int(port)))
        return endpoints

    @property
    def redis_ha_enabled(self) -> bool:
        """True when Sentinel is configured — i.e. failover is handled for us."""
        return bool(self.sentinel_endpoints)

    @property
    def redis_configured(self) -> bool:
        return bool(self.REDIS_URL) or self.redis_ha_enabled


# Singleton — import this everywhere
settings = Settings()
