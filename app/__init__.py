from app.core.config import settings
from app.core.database import db, migrate


def create_app() -> "Flask":  # noqa: F821
    """Application factory — creates and wires up the Flask app."""
    from flask import Flask
    from flask_cors import CORS
    from flask_jwt_extended import JWTManager

    app = Flask(__name__)

    # ── Configuration ─────────────────────────────────────────────────────────
    app.config["SECRET_KEY"] = settings.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = settings.DATABASE_URL
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["JWT_SECRET_KEY"] = settings.JWT_SECRET_KEY
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = settings.access_token_expires
    app.config["JWT_REFRESH_TOKEN_EXPIRES"] = settings.refresh_token_expires

    # ── Extensions ────────────────────────────────────────────────────────────
    from app.core.rate_limit import limiter

    db.init_app(app)
    migrate.init_app(app, db)
    JWTManager(app)
    limiter.init_app(app)
    _log_redis_topology()
    CORS(
        app,
        origins=settings.cors_origins_list,
        supports_credentials=True,
        # Cross-origin JS cannot read a response header unless it is exposed,
        # so without this the frontend sees the 429 but not how long to wait.
        expose_headers=[
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "RateLimit-Policy",
            "Retry-After",
        ],
    )

    # ── Import models so Alembic can detect them ──────────────────────────────
    from app.models import user, transaction  # noqa: F401

    # ── Blueprints ────────────────────────────────────────────────────────────
    from app.api.auth import auth_bp
    from app.api.health import health_bp
    from app.api.transactions import transactions_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(transactions_bp, url_prefix="/api/transactions")
    # Adds /api/health/redis and /api/health/redis/ready alongside the plain
    # /api/health liveness probe below, which is left exactly as it was.
    app.register_blueprint(health_bp, url_prefix="/api/health")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        return {
            "message": "FinTrack API is successfully deployed and running!",
            "status": "success",
            "env": settings.FLASK_ENV
        }

    @app.get("/api/health")
    def health():
        return {"status": "ok", "env": settings.FLASK_ENV}

    return app


def _log_redis_topology() -> None:
    """
    One line at boot naming the Redis mode in effect.

    Worth the startup cost: "why did this instance lose its shared rate limits"
    is otherwise answered by guessing, and the answer is nearly always that
    Sentinel was not configured on that deploy.
    """
    import logging

    from app.core.redis_ha import get_redis_ha

    log = logging.getLogger(__name__)
    ha = get_redis_ha()

    if ha is None:
        log.info("Redis not configured - rate limits are per-process only")
        return

    if ha.mode == "sentinel":
        log.info(
            "Redis HA: sentinel mode, master=%s, sentinels=%s, primary=%s",
            ha.master_name,
            [f"{h}:{p}" for h, p in ha.sentinel_endpoints],
            ha.primary_address(),
        )
    else:
        log.info(
            "Redis HA: direct mode (no Sentinel) - failover is whatever the "
            "endpoint provides"
        )
