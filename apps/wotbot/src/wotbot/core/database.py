from collections.abc import Iterator
from functools import lru_cache

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wotbot.core.config import get_settings

DatabaseConnection = psycopg.Connection[DictRow]
_MIGRATION_LOCK_KEY = (1_936_286_819, 1)


def psycopg_conninfo(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    return database_url


def sqlalchemy_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


@lru_cache
def get_connection_pool() -> ConnectionPool[DatabaseConnection]:
    return ConnectionPool(
        conninfo=psycopg_conninfo(get_settings().DATABASE_URL),
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=10,
        open=True,
    )


@lru_cache
def get_sqlalchemy_engine() -> Engine:
    return create_engine(sqlalchemy_url(get_settings().DATABASE_URL), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    factory = sessionmaker(bind=get_sqlalchemy_engine(), expire_on_commit=False)
    if get_settings().workspaces_enabled:
        from wotbot.workspaces import install_workspace_filter

        install_workspace_filter(factory)
    return factory


def get_connection() -> Iterator[DatabaseConnection]:
    """FastAPI dependency yielding a pooled Postgres connection."""
    with get_connection_pool().connection() as connection:
        yield connection


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a SQLAlchemy ORM session."""
    session_factory = get_session_factory()
    with session_factory() as session:
        yield session


def _run_alembic_upgrade() -> None:
    from importlib.resources import files

    from alembic import command
    from alembic.config import Config

    # The alembic config and migrations ship inside the ``wotbot`` package, so
    # they sit next to the code in every install mode. The ini resolves
    # ``script_location`` via ``%(here)s``, so no path overrides are needed.
    config = Config(str(files("wotbot") / "alembic.ini"))
    with psycopg.connect(psycopg_conninfo(get_settings().DATABASE_URL)) as connection:
        connection.execute("SELECT pg_advisory_lock(%s, %s)", _MIGRATION_LOCK_KEY)
        try:
            command.upgrade(config, "head")
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s, %s)", _MIGRATION_LOCK_KEY)


def init_db() -> None:
    """Create or update the Postgres schema owned by the wotbot service."""
    _run_alembic_upgrade()
