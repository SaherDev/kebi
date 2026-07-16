import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

import kebi.db.models  # noqa: F401 — registers models with Base
from alembic import context
from kebi.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Load .env for local dev; Railway sets DATABASE_URL directly in the environment.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
_url: str = os.environ["DATABASE_URL"]
# Alembic runs migrations synchronously. Pin the sync driver to psycopg
# (v3) — the only sync Postgres driver installed in production (it ships
# with the langgraph checkpointer). A bare postgresql:// URL would make
# SQLAlchemy default to psycopg2, which is a dev-only dependency: that
# exact mismatch broke the first pre-deploy migration on Railway.
if "+asyncpg" in _url:
    _url = _url.replace("+asyncpg", "+psycopg")
elif _url.startswith("postgresql://"):
    _url = _url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", _url)


# Feature 027 FR-031: exclude library-owned checkpointer tables.
# `langgraph-checkpoint-postgres` manages its own schema via
# AsyncPostgresSaver.setup(). Pure logic lives in
# `src/kebi/db/alembic_exclusion.py` so it is testable without
# booting Alembic's context.
from kebi.db.alembic_exclusion import (  # noqa: E402
    include_object as _include_object,
)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
