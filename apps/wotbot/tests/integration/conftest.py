from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
import redis

from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
    init_db,
)


@dataclass(frozen=True)
class JobDependencyUrls:
    postgres_url: str
    redis_url: str


def _close_cached_database_handles() -> None:
    if get_connection_pool.cache_info().currsize:
        get_connection_pool().close()
    get_connection_pool.cache_clear()
    get_session_factory.cache_clear()
    if get_sqlalchemy_engine.cache_info().currsize:
        get_sqlalchemy_engine().dispose()
    get_sqlalchemy_engine.cache_clear()


def _postgres_is_responsive(postgres_url: str) -> bool:
    try:
        with (
            psycopg.connect(postgres_url, connect_timeout=2) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


def _redis_is_responsive(redis_url: str) -> bool:
    client = redis.Redis.from_url(redis_url)
    try:
        return bool(client.ping())
    except redis.RedisError:
        return False
    finally:
        client.close()


@pytest.fixture(scope="session")
def docker_compose_file() -> str:
    return str(Path(__file__).with_name("docker-compose.yaml"))


@pytest.fixture(scope="session")
def docker_setup() -> list[str]:
    return ["down -v", "up --wait"]


@pytest.fixture(scope="session")
def job_dependency_urls(docker_ip: str, docker_services: Any) -> JobDependencyUrls:
    postgres_port = docker_services.port_for("postgres", 5432)
    redis_port = docker_services.port_for("valkey", 6379)
    postgres_url = f"postgresql://wotbot:wotbot@{docker_ip}:{postgres_port}/wotbot_test"
    redis_url = f"redis://{docker_ip}:{redis_port}/0"

    docker_services.wait_until_responsive(
        timeout=60.0,
        pause=0.5,
        check=lambda: _postgres_is_responsive(postgres_url),
    )
    docker_services.wait_until_responsive(
        timeout=60.0,
        pause=0.5,
        check=lambda: _redis_is_responsive(redis_url),
    )
    return JobDependencyUrls(postgres_url=postgres_url, redis_url=redis_url)


@pytest.fixture()
def jobs_integration_environment(monkeypatch, job_dependency_urls: JobDependencyUrls):
    monkeypatch.setenv("REGISTRY_DATABASE_URL", job_dependency_urls.postgres_url)
    monkeypatch.setenv("REDIS_URL", job_dependency_urls.redis_url)
    monkeypatch.setenv("SEARCH_VECTOR_DIMENSIONS", "4")

    get_settings.cache_clear()
    _close_cached_database_handles()
    init_db()
    with get_connection_pool().connection() as connection:
        connection.execute("TRUNCATE job_runs, jobs, threads RESTART IDENTITY CASCADE")
        connection.commit()

    redis_client = redis.Redis.from_url(job_dependency_urls.redis_url)
    redis_client.flushdb()
    redis_client.close()

    yield job_dependency_urls

    get_settings.cache_clear()
    _close_cached_database_handles()
    redis_client = redis.Redis.from_url(job_dependency_urls.redis_url)
    redis_client.flushdb()
    redis_client.close()
