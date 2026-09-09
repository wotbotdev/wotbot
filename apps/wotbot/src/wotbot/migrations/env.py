from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from wotbot.core.config import get_settings
from wotbot.core.database import sqlalchemy_url
from wotbot.core.orm import Base

config = context.config

if config.config_file_name is not None:
    # Keep the loggers the application configured before running migrations.
    # fileConfig disables every existing logger by default, and alembic.ini
    # pins root at WARN -- so a migration on startup silenced wotbot, httpx,
    # openai and langgraph for the life of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _import_models_for_metadata() -> None:
    """Register all SQLAlchemy models on Base.metadata for autogenerate."""
    import wotbot.agent_api.models
    import wotbot.api_keys.models
    import wotbot.catalog.credentials.models
    import wotbot.catalog.events.models
    import wotbot.catalog.models
    import wotbot.discovery.source_models
    import wotbot.jobs.db
    import wotbot.jobs.records.db
    import wotbot.panels.models
    import wotbot.search.models
    import wotbot.threads.models
    import wotbot.virtual_things.db  # noqa: F401


_import_models_for_metadata()
target_metadata = Base.metadata


def _database_url() -> str:
    return sqlalchemy_url(get_settings().DATABASE_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
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
