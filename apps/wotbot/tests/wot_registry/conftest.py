from contextlib import asynccontextmanager
import os

import pytest

from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
)
from wotbot.core.lifecycle import shutdown_backend_runtime, start_backend_runtime
from wotbot.search import set_active_search_service
from wotbot.catalog.schema import load_td_schema


class StubSearchService:
    async def search(self, query: str, k: int = 5) -> list[dict[str, object]]:
        return [
            {
                "id": "urn:thing:search-stub",
                "title": f"Stub result for {query}",
                "description": "Stubbed semantic search result",
                "tags": ["stub"],
                "score": 1.0,
                "summary": f"Matched with k={k}",
            }
        ]

    async def get_index_status(
        self,
        thing_id: str,
        document_hash: str,
    ) -> dict[str, object]:
        return {
            "thing_id": thing_id,
            "indexed": True,
            "stale": False,
            "indexed_at": "2026-03-16T00:00:00+00:00",
            "summary_source": "stub",
            "summary_model": "stub-model",
            "prompt_version": "v-test",
            "td_hash_match": bool(document_hash),
            "summary": "Stubbed semantic summary",
            "location_candidates": ["Line 3"],
            "property_names": ["temperature"],
            "action_names": ["toggle"],
            "event_names": ["overheated"],
        }

    @property
    def vector_store(self):
        return None

    async def close(self) -> None:
        return None


INIT_ADMIN_TOKEN = "test-init-admin-token"


def _close_cached_pool() -> None:
    if get_connection_pool.cache_info().currsize:
        get_connection_pool().close()
    get_connection_pool.cache_clear()
    get_session_factory.cache_clear()
    if get_sqlalchemy_engine.cache_info().currsize:
        get_sqlalchemy_engine().dispose()
    get_sqlalchemy_engine.cache_clear()


@asynccontextmanager
async def _registry_only_lifespan(app):
    from wotbot.core.database import init_db

    settings = get_settings()
    app.state.settings = settings
    init_db()
    connection_pool = get_connection_pool()
    await start_backend_runtime(
        app,
        settings=settings,
        connection_pool=connection_pool,
    )
    try:
        yield
    finally:
        await shutdown_backend_runtime(app)


@pytest.fixture(autouse=True)
def clear_backend_state(monkeypatch):
    test_database_url = os.getenv("WOTBOT_TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("WOTBOT_TEST_DATABASE_URL is required for Postgres registry tests")

    monkeypatch.setenv("REGISTRY_DATABASE_URL", test_database_url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("INIT_ADMIN_TOKEN", INIT_ADMIN_TOKEN)
    monkeypatch.setenv("REGISTRY_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    monkeypatch.setenv("SEARCH_VECTOR_DIMENSIONS", "4")
    monkeypatch.setenv("WOT_RUNTIME_REGISTRY_TOKEN", "test-runtime-registry-token")
    monkeypatch.setenv("WOT_RUNTIME_API_TOKEN", "test-runtime-api-token")

    get_settings.cache_clear()
    _close_cached_pool()
    load_td_schema.cache_clear()
    from wotbot.core.database import init_db

    init_db()
    init_pool = get_connection_pool()
    with init_pool.connection() as connection:
        connection.execute(
            """
            TRUNCATE api_keys, things, thing_credentials, thing_event_outbox,
                search_index_chunks, threads, job_runs, jobs
            RESTART IDENTITY CASCADE
            """
        )
        connection.commit()
    yield
    get_settings.cache_clear()
    _close_cached_pool()
    load_td_schema.cache_clear()


@pytest.fixture(autouse=True)
def stub_search_runtime(monkeypatch):
    def fake_start_search_service(app, *, settings):
        service = StubSearchService()
        app.state.search_service = service
        set_active_search_service(service)

    async def fake_stop_search_service(app):
        app.state.search_service = None
        set_active_search_service(None)

    monkeypatch.setattr("wotbot.core.lifecycle.start_search_service", fake_start_search_service)
    monkeypatch.setattr("wotbot.core.lifecycle.stop_search_service", fake_stop_search_service)
    set_active_search_service(None)
    yield
    set_active_search_service(None)


@pytest.fixture(autouse=True)
def registry_only_app_lifespan(clear_backend_state, stub_search_runtime):
    from wotbot.api.main import app

    original_lifespan_context = app.router.lifespan_context
    app.router.lifespan_context = _registry_only_lifespan
    yield
    app.router.lifespan_context = original_lifespan_context


@pytest.fixture
def authenticated_headers():
    return {"Authorization": f"Bearer {INIT_ADMIN_TOKEN}"}
