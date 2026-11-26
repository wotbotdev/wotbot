"""Onboarding a known resource by external ID over HTTP, on Postgres."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_tx_bootstrap_discovery import federated_entry
from wotbot.api_keys.store import create_api_key
from wotbot.catalog.router import router as things_router
from wotbot.core.config import get_settings
from wotbot.core.database import (
    get_connection_pool,
    get_session_factory,
    get_sqlalchemy_engine,
    init_db,
)
from wotbot.discovery.models import OnboardingResult
from wotbot.discovery.providers.base import provider_thing_id
from wotbot.discovery.providers.tx_bootstrap import TxBootstrapProvider
from wotbot.discovery.routes import router as discovery_router
from wotbot.workspaces.store import create_workspace

pytestmark = pytest.mark.skipif(
    not os.getenv("WOTBOT_TEST_DATABASE_URL"),
    reason="WOTBOT_TEST_DATABASE_URL is required for Postgres workspace tests",
)

_SCOPES = ["sources:manage", "things:write", "things:read", "credentials:write"]
_PARTICIPANT = "https://consumer.example"


def _close_cached_handles() -> None:
    if get_connection_pool.cache_info().currsize:
        get_connection_pool().close()
    get_connection_pool.cache_clear()
    get_session_factory.cache_clear()
    if get_sqlalchemy_engine.cache_info().currsize:
        get_sqlalchemy_engine().dispose()
    get_sqlalchemy_engine.cache_clear()


async def _fake_onboarding_document(self, source, candidate, *, runtime):
    thing_id = provider_thing_id(candidate.provider, candidate.source_id, candidate.external_id)
    return OnboardingResult(
        document={
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": thing_id,
            "title": candidate.title,
            "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
            "security": "nosec_sc",
        }
    )


class WorkspaceOnboardingTestCase(unittest.TestCase):
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
        with get_session_factory()() as session:
            for workspace_id in ("alpha", "beta"):
                create_workspace(session, workspace_id=workspace_id, title=workspace_id)
            session.commit()
            self.alpha = self._bearer(session, "alpha")
            self.beta = self._bearer(session, "beta")
        app = FastAPI()
        app.include_router(discovery_router)
        app.include_router(things_router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        _close_cached_handles()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    @staticmethod
    def _bearer(session, workspace_id: str) -> dict[str, str]:
        _, raw_key = create_api_key(
            session, user_id="admin", name=workspace_id, scopes=_SCOPES, workspace_id=workspace_id
        )
        return {"Authorization": f"Bearer {raw_key}"}

    def _register(self, headers: dict[str, str], *, with_credential: bool = True) -> str:
        response = self.client.post(
            "/api/discovery/sources",
            headers=headers,
            json={
                "provider": "tx-bootstrap",
                "title": "Dataspace",
                "config": {"participant_api_url": _PARTICIPANT},
                "security": {"scheme": "bearer"},
            },
        )
        self.assertEqual(response.status_code, 428, response.text)
        challenge = response.json()["detail"]
        if with_credential:
            stored = self.client.put(
                f"/api/discovery/sources/{challenge['source_id']}"
                f"/credentials/{challenge['security_name']}",
                headers=headers,
                json={"scheme": "bearer", "credentials": {"token": "participant-token"}},
            )
            self.assertEqual(stored.status_code, 200, stored.text)
        return challenge["source_id"]

    def _onboard(self, headers: dict[str, str], source_id: str, external_id: str = "entry-1"):
        with (
            patch(
                "wotbot.discovery.providers.tx_bootstrap.source_json",
                new=AsyncMock(return_value=federated_entry()),
            ),
            patch.object(TxBootstrapProvider, "onboarding_document", _fake_onboarding_document),
        ):
            return self.client.post(
                f"/api/discovery/sources/{source_id}/onboard",
                headers=headers,
                json={"external_id": external_id},
            )

    def test_a_catalog_entry_is_onboarded_once_into_the_caller_workspace(self) -> None:
        source_id = self._register(self.alpha)

        first = self._onboard(self.alpha, source_id)
        second = self._onboard(self.alpha, source_id)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        thing_id = provider_thing_id("tx-bootstrap", source_id, "entry-1")
        self.assertEqual(first.json()["thing"]["id"], thing_id)
        self.assertEqual(second.json()["thing"]["id"], thing_id)

        alpha_things = self.client.get(
            "/api/things", headers=self.alpha, params={"origin_kind": "discovery"}
        ).json()["items"]
        self.assertEqual([thing["origin"]["external_id"] for thing in alpha_things], ["entry-1"])
        beta_things = self.client.get("/api/things", headers=self.beta).json()["items"]
        self.assertEqual(beta_things, [])

    def test_another_workspace_cannot_onboard_from_the_source(self) -> None:
        source_id = self._register(self.alpha)
        response = self._onboard(self.beta, source_id)
        self.assertEqual(response.status_code, 404, response.text)

    def test_missing_credentials_are_reported_as_a_challenge(self) -> None:
        source_id = self._register(self.alpha, with_credential=False)
        response = self._onboard(self.alpha, source_id)
        self.assertEqual(response.status_code, 428, response.text)
        self.assertEqual(response.json()["detail"]["source_id"], source_id)

    def test_providers_without_lookup_by_id_are_rejected(self) -> None:
        registered = self.client.post(
            "/api/discovery/sources",
            headers=self.alpha,
            json={"provider": "udata", "config": {"url": "https://data.example"}},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        source_id = registered.json()["source"]["source_id"]

        response = self._onboard(self.alpha, source_id, external_id="dataset-1")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("cannot onboard resources by ID", response.json()["detail"])
