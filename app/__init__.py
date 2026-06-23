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
    db.init_app(app)
    migrate.init_app(app, db)
    JWTManager(app)
    CORS(app, origins=settings.cors_origins_list, supports_credentials=True)

    # ── Import models so Alembic can detect them ──────────────────────────────
    from app.models import user, transaction  # noqa: F401

    # ── Blueprints ────────────────────────────────────────────────────────────
    from app.api.auth import auth_bp
    from app.api.transactions import transactions_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(transactions_bp, url_prefix="/api/transactions")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return {"status": "ok", "env": settings.FLASK_ENV}

    return app
