"""Workspace registry, API key binding and per-request isolation on Postgres."""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from wotbot.agent_api.runtime import AgentRuntime
from wotbot.api_keys.router import router as api_keys_router
from wotbot.api_keys.service import ApiKeyManagementService
from wotbot.api_keys.store import create_api_key
from wotbot.auth.models import User
from wotbot.auth.providers import get_agent_principal, get_api_key_user_from_token
from wotbot.auth.router import router as me_router
from wotbot.catalog.models import Thing
from wotbot.catalog.router import router as things_router
from wotbot.catalog.store import create_thing
from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
    init_db,
)
from wotbot.workspaces import (
    ALL_WORKSPACES,
    InvalidWorkspaceIdError,
    current_workspace_id,
    workspace_scope,
)
from wotbot.workspaces.router import router as workspaces_router
from wotbot.workspaces.store import (
    WorkspaceConflictError,
    WorkspaceNotEmptyError,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("WOTBOT_TEST_DATABASE_URL"),
    reason="WOTBOT_TEST_DATABASE_URL is required for Postgres workspace tests",
)

_SERVICE_TOKEN = "test-runtime-registry-token"
_ENV = {
    "WORKSPACES_ENABLED": "true",
    "WOT_RUNTIME_REGISTRY_TOKEN": _SERVICE_TOKEN,
}


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


class WorkspaceAccessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        names = ("REGISTRY_DATABASE_URL", *_ENV)
        self._saved_env = {name: os.environ.get(name) for name in names}
        os.environ["REGISTRY_DATABASE_URL"] = os.environ["WOTBOT_TEST_DATABASE_URL"]
        os.environ.update(_ENV)
        get_settings.cache_clear()
        _close_cached_handles()
        init_db()
        with get_connection_pool().connection() as connection:
            connection.execute(
                "TRUNCATE api_keys, things, discovery_source_credentials, discovery_sources CASCADE"
            )
            connection.execute("DELETE FROM workspaces WHERE id <> 'default'")
            connection.commit()
        self.sessions = get_session_factory()
        with self.sessions() as session:
            for workspace_id in ("alpha", "beta"):
                create_workspace(session, workspace_id=workspace_id, title=workspace_id.title())
            session.commit()

    def tearDown(self) -> None:
        _close_cached_handles()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    # -- helpers -----------------------------------------------------------

    def _key(self, workspace_id: str, scopes: list[str]) -> str:
        with self.sessions() as session:
            _, raw_key = create_api_key(
                session,
                user_id="admin",
                name=f"{workspace_id} key",
                scopes=scopes,
                workspace_id=workspace_id,
            )
            return raw_key

    def _client(self) -> TestClient:
        app = FastAPI()
        for router in (things_router, me_router, workspaces_router, api_keys_router):
            app.include_router(router)
        return TestClient(app)

    @staticmethod
    def _bearer(raw_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {raw_key}"}

    def _thing_ids(self, client: TestClient, headers: dict[str, str]) -> list[str]:
        response = client.get("/api/things", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return sorted(item["id"] for item in response.json()["items"])

    # -- registry ----------------------------------------------------------

    def test_registry_lists_creates_and_rejects_duplicates(self) -> None:
        with self.sessions() as session:
            self.assertEqual(
                [record.id for record in list_workspaces(session)], ["alpha", "beta", "default"]
            )
            with self.assertRaises(WorkspaceConflictError):
                create_workspace(session, workspace_id="alpha", title="Again")
            session.rollback()
            with self.assertRaises(InvalidWorkspaceIdError):
                create_workspace(session, workspace_id="Not Valid", title="Bad")

    def test_only_empty_non_default_workspaces_can_be_deleted(self) -> None:
        self._key("alpha", ["things:read"])
        with self.sessions() as session:
            with self.assertRaises(ValueError):
                delete_workspace(session, "default")
            with self.assertRaisesRegex(WorkspaceNotEmptyError, "1 API keys"):
                delete_workspace(session, "alpha")
            self.assertTrue(delete_workspace(session, "beta"))
            self.assertFalse(delete_workspace(session, "beta"))
            session.commit()
            self.assertIsNone(get_workspace(session, "beta"))

    # -- key creation rules --------------------------------------------------

    def test_workspace_managers_can_issue_keys_for_any_workspace(self) -> None:
        admin = User(user_id="admin", scopes=None, workspace_id="default")
        with self.sessions() as session:
            record, _ = ApiKeyManagementService(session).create_for_user(
                user=admin, name="uco", scopes=["things:read"], workspace_id="alpha"
            )
            self.assertEqual(record.workspace_id, "alpha")
            with self.assertRaises(HTTPException) as unknown:
                ApiKeyManagementService(session).create_for_user(
                    user=admin, name="x", scopes=["things:read"], workspace_id="gamma"
                )
            self.assertEqual(unknown.exception.status_code, 422)

    def test_other_keys_stay_in_their_own_workspace(self) -> None:
        alpha_admin = User(
            user_id="alpha-admin",
            scopes=["keys:manage", "things:read"],
            workspace_id="alpha",
        )
        with self.sessions() as session:
            service = ApiKeyManagementService(session)
            record, _ = service.create_for_user(user=alpha_admin, name="k", scopes=["things:read"])
            self.assertEqual(record.workspace_id, "alpha")
            with self.assertRaises(HTTPException) as escape:
                service.create_for_user(
                    user=alpha_admin, name="k", scopes=["things:read"], workspace_id="beta"
                )
            self.assertEqual(escape.exception.status_code, 403)

    def test_keys_resolve_to_their_workspace(self) -> None:
        raw_key = self._key("alpha", ["agent:invoke"])
        user = get_api_key_user_from_token(raw_key, session_factory=self.sessions)
        self.assertEqual(user.workspace_id, "alpha")
        principal = get_agent_principal(user.api_key_id, session_factory=self.sessions)
        self.assertEqual(principal.workspace_id, "alpha")

    # -- per-request isolation over HTTP -----------------------------------

    def test_requests_only_see_their_workspace(self) -> None:
        for workspace_id in ("default", "alpha", "beta"):
            with workspace_scope(workspace_id), self.sessions() as session:
                create_thing(session, _td(f"urn:test:{workspace_id}-lamp"))
                session.commit()

        client = self._client()
        alpha = self._bearer(self._key("alpha", ["things:read"]))
        default = self._bearer(self._key("default", ["things:read"]))
        service = {"X-Registry-Service": "wot_runtime", "X-Registry-Service-Token": _SERVICE_TOKEN}

        self.assertEqual(self._thing_ids(client, alpha), ["urn:test:alpha-lamp"])
        self.assertEqual(self._thing_ids(client, default), ["urn:test:default-lamp"])
        self.assertEqual(
            self._thing_ids(client, service),
            ["urn:test:alpha-lamp", "urn:test:beta-lamp", "urn:test:default-lamp"],
        )
        response = client.get("/api/things/urn:test:beta-lamp", headers=alpha)
        self.assertEqual(response.status_code, 404)

    def test_writes_land_in_the_key_workspace(self) -> None:
        client = self._client()
        alpha = self._bearer(self._key("alpha", ["things:read", "things:write"]))
        response = client.post("/api/things", headers=alpha, json=_td("urn:test:new"))
        self.assertEqual(response.status_code, 201, response.text)

        with self.sessions() as session:
            stored = session.scalar(
                select(Thing).where(Thing.id == "urn:test:new"),
                execution_options={ALL_WORKSPACES: True},
            )
            self.assertEqual(stored.workspace_id, "alpha")

    def test_me_reports_the_workspace(self) -> None:
        client = self._client()
        response = client.get("/api/me", headers=self._bearer(self._key("beta", ["things:read"])))
        self.assertEqual(response.json()["workspace_id"], "beta")

    def test_workspace_api_requires_the_manage_scope(self) -> None:
        client = self._client()
        manager = self._bearer(self._key("default", ["workspaces:manage", "keys:manage"]))
        plain = self._bearer(self._key("alpha", ["things:read"]))

        self.assertEqual(client.get("/api/workspaces", headers=plain).status_code, 403)
        created = client.post(
            "/api/workspaces", headers=manager, json={"id": "energie", "title": "Energie"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        key = client.post(
            "/api/keys",
            headers=manager,
            json={"name": "uco", "scopes": ["keys:manage"], "workspace_id": "energie"},
        )
        self.assertEqual(key.status_code, 201, key.text)
        self.assertEqual(key.json()["key"]["workspace_id"], "energie")
        self.assertEqual(client.delete("/api/workspaces/energie", headers=manager).status_code, 409)

    # -- agent runs (A2A and MCP) -------------------------------------------

    def test_agent_runs_bind_the_invoking_key_workspace(self) -> None:
        raw_key = self._key("beta", ["agent:invoke"])
        owner = get_api_key_user_from_token(raw_key, session_factory=self.sessions).api_key_id
        runtime = SimpleNamespace(store=SimpleNamespace(session_factory=self.sessions))

        async def run(task_owner: str) -> str:
            await AgentRuntime._bind_owner_workspace(runtime, task_owner)
            return current_workspace_id()

        self.assertEqual(asyncio.run(run(owner)), "beta")
        with self.assertRaises(PermissionError):
            asyncio.run(run("revoked-or-unknown-key"))
