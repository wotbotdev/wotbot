"""Workspace isolation of the semantic search index on Postgres."""

from __future__ import annotations

import asyncio
import hashlib
import os
import unittest
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.embeddings import Embeddings
from sqlalchemy import select

from wotbot.catalog.store import create_thing
from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
    init_db,
)
from wotbot.rdf.api import _load_all_things
from wotbot.search.models import SearchIndexChunk
from wotbot.search.vector_store import SearchIndexDocument, SearchVectorStore
from wotbot.workspaces import ALL_WORKSPACES, workspace_scope

pytestmark = pytest.mark.skipif(
    not os.getenv("WOTBOT_TEST_DATABASE_URL"),
    reason="WOTBOT_TEST_DATABASE_URL is required for Postgres workspace tests",
)


class _HashEmbeddings(Embeddings):
    """Deterministic, offline embeddings: one hot dimension per text hash."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        vector[int(hashlib.sha256(text.encode()).hexdigest(), 16) % self.dimensions] = 1.0
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _close_cached_handles() -> None:
    if get_connection_pool.cache_info().currsize:
        get_connection_pool().close()
    get_connection_pool.cache_clear()
    get_session_factory.cache_clear()
    if get_sqlalchemy_engine.cache_info().currsize:
        get_sqlalchemy_engine().dispose()
    get_sqlalchemy_engine.cache_clear()


def _td(thing_id: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": thing_id.rsplit(":", 1)[-1],
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
    }


class WorkspaceSearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        names = ("REGISTRY_DATABASE_URL", "WORKSPACES_ENABLED")
        self._saved_env = {name: os.environ.get(name) for name in names}
        os.environ["REGISTRY_DATABASE_URL"] = os.environ["WOTBOT_TEST_DATABASE_URL"]
        os.environ["WORKSPACES_ENABLED"] = "true"
        get_settings.cache_clear()
        _close_cached_handles()
        init_db()
        with get_connection_pool().connection() as connection:
            connection.execute(
                "TRUNCATE api_keys, things, discovery_source_credentials, discovery_sources, "
                "search_index_chunks CASCADE"
            )
            connection.execute("DELETE FROM workspaces WHERE id <> 'default'")
            connection.commit()
        self.sessions = get_session_factory()
        self.dimensions = get_settings().SEARCH_VECTOR_DIMENSIONS
        self.store = SearchVectorStore(
            embeddings=_HashEmbeddings(self.dimensions),
            embedding_dimensions=self.dimensions,
            session_factory=self.sessions,
        )

    def tearDown(self) -> None:
        _close_cached_handles()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    def _index(self, workspace_id: str, thing_id: str) -> None:
        async def index() -> None:
            with workspace_scope(workspace_id):
                await self.store.replace_thing_chunks(
                    thing_id,
                    [(f"{thing_id}#0", SearchIndexDocument(f"{thing_id} lamp", {"id": thing_id}))],
                )

        asyncio.run(index())

    def _in(self, workspace_id: str, coroutine_factory):
        async def run():
            with workspace_scope(workspace_id):
                return await coroutine_factory()

        return asyncio.run(run())

    def test_search_only_returns_the_active_workspace(self) -> None:
        for workspace_id in ("default", "alpha", "beta"):
            self._index(workspace_id, f"urn:test:{workspace_id}-lamp")

        matches = self._in("alpha", lambda: self.store.query_similar("lamp", limit=10))
        self.assertEqual([match.metadata["id"] for match in matches], ["urn:test:alpha-lamp"])

        chunk = self._in("alpha", lambda: self.store.get_device_chunk("urn:test:beta-lamp"))
        self.assertIsNone(chunk)

    def test_deleting_from_another_workspace_leaves_the_chunk(self) -> None:
        self._index("beta", "urn:test:beta-lamp")
        self._in("alpha", lambda: self.store.delete_thing_chunks("urn:test:beta-lamp"))
        chunk = self._in("beta", lambda: self.store.get_device_chunk("urn:test:beta-lamp"))
        self.assertIsNotNone(chunk)

    def test_reindexing_in_the_same_workspace_replaces_the_chunk(self) -> None:
        self._index("alpha", "urn:test:alpha-lamp")
        self._index("alpha", "urn:test:alpha-lamp")
        with self.sessions() as session:
            rows = session.scalars(
                select(SearchIndexChunk), execution_options={ALL_WORKSPACES: True}
            ).all()
            self.assertEqual(
                [(row.thing_id, row.workspace_id) for row in rows],
                [("urn:test:alpha-lamp", "alpha")],
            )

    def test_rdf_reindex_still_loads_every_workspace(self) -> None:
        for workspace_id in ("default", "alpha"):
            with workspace_scope(workspace_id), self.sessions() as session:
                create_thing(session, _td(f"urn:test:{workspace_id}-lamp"))
                session.commit()

        thing_ids = sorted(thing_id for thing_id, _ in _load_all_things())
        self.assertEqual(thing_ids, ["urn:test:alpha-lamp", "urn:test:default-lamp"])

    def test_migration_moves_existing_chunks_to_their_thing_workspace(self) -> None:
        config = Config(str(files("wotbot") / "alembic.ini"))
        command.downgrade(config, "0013_workspace_registry")
        with workspace_scope("alpha"), self.sessions() as session:
            create_thing(session, _td("urn:test:alpha-lamp"))
            session.commit()
        embedding = "[" + ",".join(["1"] + ["0"] * (self.dimensions - 1)) + "]"
        with get_connection_pool().connection() as connection:
            for chunk_id, thing_id in (("c1", "urn:test:alpha-lamp"), ("c2", "urn:test:gone")):
                connection.execute(
                    "INSERT INTO search_index_chunks (chunk_id, thing_id, page_content, embedding)"
                    " VALUES (%s, %s, 'lamp', %s::vector)",
                    (chunk_id, thing_id, embedding),
                )
            connection.commit()

        command.upgrade(config, "head")

        with self.sessions() as session:
            rows = session.scalars(
                select(SearchIndexChunk).order_by(SearchIndexChunk.chunk_id),
                execution_options={ALL_WORKSPACES: True},
            ).all()
            self.assertEqual(
                [(row.chunk_id, row.workspace_id) for row in rows],
                [("c1", "alpha"), ("c2", "default")],
            )
