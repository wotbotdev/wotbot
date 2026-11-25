"""Workspace isolation of the Thing catalog and discovery sources on Postgres."""

from __future__ import annotations

import hashlib
import os
import unittest
from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

from wotbot.catalog.models import Thing, ThingConflictError
from wotbot.catalog.store import create_thing, get_thing, list_things, put_thing
from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
    init_db,
)
from wotbot.discovery.service import _SOURCE_ID_HEX_DIGITS, _source_id
from wotbot.discovery.source_models import SourceRecord
from wotbot.discovery.source_store import (
    get_source,
    get_source_credential,
    insert_source,
    list_sources,
    search_sources_page,
    set_source_credential,
)
from wotbot.workspaces import ALL_WORKSPACES, workspace_filter_installed, workspace_scope

pytestmark = pytest.mark.skipif(
    not os.getenv("WOTBOT_TEST_DATABASE_URL"),
    reason="WOTBOT_TEST_DATABASE_URL is required for Postgres workspace tests",
)

_PROVIDER = "udata"
_CATALOG = "https://data.example"


def _close_cached_handles() -> None:
    if get_connection_pool.cache_info().currsize:
        get_connection_pool().close()
    get_connection_pool.cache_clear()
    get_session_factory.cache_clear()
    if get_sqlalchemy_engine.cache_info().currsize:
        get_sqlalchemy_engine().dispose()
    get_sqlalchemy_engine.cache_clear()


def _alembic_config() -> Config:
    return Config(str(files("wotbot") / "alembic.ini"))


def _td(thing_id: str, title: str = "Lamp") -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": title,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
    }


def _source() -> SourceRecord:
    return SourceRecord(
        id=_source_id(_PROVIDER, _CATALOG),
        provider=_PROVIDER,
        external_id=_CATALOG,
        title="Example data",
        description="",
        tags=[],
        config={"url": _CATALOG},
        network_access="public",
        security_name="source_sc",
        security_scheme="apikey",
    )


class WorkspaceCatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {
            name: os.environ.get(name) for name in ("REGISTRY_DATABASE_URL", "WORKSPACES_ENABLED")
        }
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

    def tearDown(self) -> None:
        _close_cached_handles()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    def _add_source(self, workspace_id: str) -> SourceRecord:
        with workspace_scope(workspace_id), self.sessions() as session:
            record = insert_source(session, _source())
            session.commit()
            return record

    def _add_thing(self, workspace_id: str, thing_id: str) -> None:
        with workspace_scope(workspace_id), self.sessions() as session:
            create_thing(session, _td(thing_id))
            session.commit()

    def test_session_factory_is_filtered_when_enabled(self) -> None:
        self.assertTrue(workspace_filter_installed(self.sessions))

    def test_default_workspace_keeps_the_original_source_id(self) -> None:
        identity = f"{_PROVIDER}\0source\0{_CATALOG}".encode()
        digest = hashlib.sha256(identity).hexdigest()[:_SOURCE_ID_HEX_DIGITS]
        self.assertEqual(_source_id(_PROVIDER, _CATALOG), f"urn:wotbot:source:{_PROVIDER}:{digest}")

    def test_each_workspace_gets_its_own_source_id(self) -> None:
        default_id = _source_id(_PROVIDER, _CATALOG)
        with workspace_scope("alpha"):
            alpha_id = _source_id(_PROVIDER, _CATALOG)
        with workspace_scope("beta"):
            beta_id = _source_id(_PROVIDER, _CATALOG)
        self.assertEqual(len({default_id, alpha_id, beta_id}), 3)

    def test_two_workspaces_can_register_the_same_catalog(self) -> None:
        alpha = self._add_source("alpha")
        beta = self._add_source("beta")
        self.assertNotEqual(alpha.id, beta.id)

        with workspace_scope("alpha"), self.sessions() as session:
            self.assertEqual([source.id for source in list_sources(session)], [alpha.id])
            self.assertIsNone(get_source(session, beta.id))
            records, total = search_sources_page(session, query="example")
            self.assertEqual(([record.id for record in records], total), ([alpha.id], 1))

    def test_things_are_only_visible_in_their_workspace(self) -> None:
        self._add_thing("alpha", "urn:test:alpha-lamp")
        self._add_thing("beta", "urn:test:beta-lamp")

        with workspace_scope("alpha"), self.sessions() as session:
            records, total = list_things(session)
            self.assertEqual(
                ([record.id for record in records], total), (["urn:test:alpha-lamp"], 1)
            )
            self.assertIsNone(get_thing(session, "urn:test:beta-lamp"))

        with self.sessions() as session:
            every = session.scalars(select(Thing), execution_options={ALL_WORKSPACES: True}).all()
            self.assertEqual(len(every), 2)

    def test_put_never_overwrites_a_thing_of_another_workspace(self) -> None:
        self._add_thing("alpha", "urn:test:shared")

        with workspace_scope("beta"), self.sessions() as session:
            with self.assertRaises(ThingConflictError):
                put_thing(session, "urn:test:shared", _td("urn:test:shared", title="Hijacked"))
            session.rollback()

        with workspace_scope("alpha"), self.sessions() as session:
            record = get_thing(session, "urn:test:shared")
            self.assertIsNotNone(record)
            self.assertEqual(record.title, "Lamp")

    def test_put_creates_then_updates_within_a_workspace(self) -> None:
        with workspace_scope("alpha"), self.sessions() as session:
            _, created = put_thing(session, "urn:test:lamp", _td("urn:test:lamp"))
            _, created_again = put_thing(session, "urn:test:lamp", _td("urn:test:lamp", "Renamed"))
            session.commit()
            self.assertEqual((created, created_again), (True, False))
            record = get_thing(session, "urn:test:lamp")
            self.assertEqual((record.title, record.id), ("Renamed", "urn:test:lamp"))

    def test_credentials_stay_in_their_workspace(self) -> None:
        alpha = self._add_source("alpha")
        with workspace_scope("alpha"), self.sessions() as session:
            set_source_credential(
                session,
                source_id=alpha.id,
                security_name="source_sc",
                scheme="apikey",
                credentials={"key": "alpha-secret"},
            )
            session.commit()

        with workspace_scope("beta"), self.sessions() as session:
            self.assertIsNone(
                get_source_credential(session, source_id=alpha.id, security_name="source_sc")
            )
            with self.assertRaises(ValueError):
                set_source_credential(
                    session,
                    source_id=alpha.id,
                    security_name="source_sc",
                    scheme="apikey",
                    credentials={"key": "beta-overwrite"},
                )
            session.rollback()

        with workspace_scope("alpha"), self.sessions() as session:
            stored = get_source_credential(session, source_id=alpha.id, security_name="source_sc")
            self.assertEqual(stored.credentials, {"key": "alpha-secret"})

    def test_models_and_migrations_have_no_schema_drift(self) -> None:
        command.check(_alembic_config())

    def test_downgrade_refuses_to_merge_workspaces(self) -> None:
        self._add_thing("alpha", "urn:test:alpha-lamp")
        with self.assertRaises(RuntimeError):
            command.downgrade(_alembic_config(), "0011_panel_narrow_screenshot")

        with self.sessions() as session:
            session.execute(delete(Thing), execution_options={ALL_WORKSPACES: True})
            session.commit()
        command.downgrade(_alembic_config(), "0011_panel_narrow_screenshot")
        command.upgrade(_alembic_config(), "head")
