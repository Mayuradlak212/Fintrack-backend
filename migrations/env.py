from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Import app + models so Alembic can autogenerate ──────────────────────────
from app import create_app
from app.core.database import db
from app.core.config import settings

# this is the Alembic Config object
config = context.config

# Override sqlalchemy.url from our Pydantic settings
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

import os
if config.config_file_name is not None:
    ini_path = config.config_file_name
    if not os.path.exists(ini_path):
        ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    if os.path.exists(ini_path):
        fileConfig(ini_path)

# Ensure all models are imported so autogenerate detects them
flask_app = create_app()
with flask_app.app_context():
    target_metadata = db.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
