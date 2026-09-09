from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, TypedDict
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from wotbot.agent.prompts.discovery import DISCOVERY_PROMPT
from wotbot.agent.tools.external_discovery import (
    discover_external,
    onboard_candidate,
    register_external_source,
    sources_search,
)
from wotbot.auth.dependencies import require_user
from wotbot.auth.models import User
from wotbot.catalog.models import ThingRecord
from wotbot.core.settings import Settings
from wotbot.discovery.errors import (
    CredentialChallengeError,
    SourceConfigurationError,
    SourceProtocolError,
    SourceUnavailableError,
    UnsafeUrlError,
)
from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import (
    CandidateDraft,
    CandidateRecord,
    DownloadRecord,
    ProviderResponse,
    SourceDefinition,
)
from wotbot.discovery.providers import PROVIDERS, ToolHiveProvider
from wotbot.discovery.providers.base import (
    credential_headers,
    dataset_document,
    provider_action_href,
    provider_download_action,
)
from wotbot.discovery.providers.udata import resources
from wotbot.discovery.routes import _download_response, _registration_result, router
from wotbot.discovery.search import prepare_search_intent
from wotbot.discovery.service import DiscoveryService
from wotbot.discovery.source_models import SourceRecord
from wotbot.discovery.store import CandidateStore, DownloadStore


class FakePipeline:
    """Collects buffered writes so a batched store still records TTLs."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._writes: list[tuple[str, str, int]] = []

    def set(self, key: str, value: str, *, ex: int) -> FakePipeline:
        self._writes.append((key, value, ex))
        return self

    async def execute(self) -> list[bool]:
        for key, value, ex in self._writes:
            self._redis.values[key] = value
            self._redis.expiries[key] = ex
        written, self._writes = len(self._writes), []
        return [True] * written


class FakeRedis:
    values: ClassVar[dict[str, str]] = {}
    expiries: ClassVar[dict[str, int]] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        del transaction
        return FakePipeline(self)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expiries[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def aclose(self) -> None:
        return None


def source_record(
    *,
    source_id: str = "urn:wotbot:source:udata:test",
    provider: str = "udata",
    scheme: str = "nosec",
) -> SourceRecord:
    return SourceRecord(
        id=source_id,
        provider=provider,
        external_id="https://data.example",
        title="Luxembourg open data",
        description="National public data catalog",
        tags=["Luxembourg", "open data"],
        config={"url": "https://data.example"},
        network_access="public",
        security_name="source_sc",
        security_scheme=scheme,
    )


def thing_record(
    *,
    source_id: str = "urn:wotbot:source:udata:test",
    provider: str = "udata",
) -> ThingRecord:
    action_name = "download_csv_deadbeef"
    thing_id = "urn:wotbot:external:udata:roads"
    return ThingRecord(
        id=thing_id,
        title="Roads",
        description="Road network",
        tags=[],
        origin_kind="discovery",
        origin_provider=provider,
        origin_external_id="roads",
        origin_source_id=source_id,
        document={
            "id": thing_id,
            "actions": {
                action_name: provider_download_action(
                    thing_id,
                    action_name=action_name,
                    title="Download CSV",
                    description="Roads",
                    resource={"id": "csv", "media_type": "text/csv"},
                )
            },
        },
        document_hash="hash",
    )


class SourceRegistrationPermissionsTestCase(unittest.TestCase):
    def test_registration_routes_only_allow_thing_creation_with_write_scope(self) -> None:
        body = {"provider": "openapi", "config": {"url": "https://api.example/openapi.json"}}
        for write_allowed in (False, True):
            user = User(
                user_id="source-manager",
                scopes=["sources:manage", *(["things:write"] if write_allowed else [])],
            )
            app = FastAPI()
            app.state.settings = Settings()
            app.include_router(router)
            app.dependency_overrides[require_user] = lambda: user
            for method, path, payload, service_method in (
                ("POST", "/api/discovery/sources", body, "register_source"),
                (
                    "POST",
                    "/api/discovery/sources/detect",
                    {"url": "https://api.example"},
                    "register_source_url",
                ),
                ("PUT", "/api/discovery/sources/source-a", body, "update_registered_source"),
            ):
                with self.subTest(write_allowed=write_allowed, path=path):
                    register = AsyncMock(return_value={"source": {"source_id": "source-a"}})
                    with (
                        patch.object(DiscoveryService, service_method, new=register),
                        TestClient(app) as client,
                    ):
                        response = client.request(method, path, json=payload)
                    self.assertIn(response.status_code, (200, 201))
                    self.assertEqual(register.await_args.kwargs["allow_onboarding"], write_allowed)


class CandidateContractTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_are_thread_scoped_and_public_links_are_bounded(self) -> None:
        record = CandidateRecord.from_draft(
            CandidateDraft(
                provider="udata",
                source_id="urn:wotbot:source:udata:test",
                external_id="roads",
                kind="dataset",
                title="Roads",
                links=tuple(
                    [{"title": "unsafe", "url": "javascript:alert(1)"}]
                    + [
                        {"title": str(index), "url": f"https://data.example/{index}"}
                        for index in range(10)
                    ]
                ),
            ),
            scope_kind="thread",
            scope_id="thread-a",
        )
        self.assertEqual(len(record.links), 6)
        self.assertEqual(record.public("candidate")["capabilities"], ["onboard"])

    async def test_candidate_and_download_ttls_are_bounded(self) -> None:
        FakeRedis.values = {}
        FakeRedis.expiries = {}
        candidate_store = CandidateStore("redis://unused", ttl_seconds=1800)
        candidate = CandidateRecord.from_draft(
            CandidateDraft(
                provider="udata",
                source_id="source-a",
                external_id="roads",
                kind="dataset",
                title="Roads",
            ),
            scope_kind="thread",
            scope_id="thread-a",
        )
        with patch("wotbot.discovery.store.redis.from_url", return_value=FakeRedis()):
            candidate_id = await candidate_store.put(candidate)
            self.assertEqual(
                await candidate_store.get(
                    candidate_id,
                    scope_kind="thread",
                    scope_id="thread-a",
                ),
                candidate,
            )
            with self.assertRaisesRegex(ValueError, "different discovery scope"):
                await candidate_store.get(
                    candidate_id,
                    scope_kind="thread",
                    scope_id="thread-b",
                )
        self.assertEqual(next(iter(FakeRedis.expiries.values())), 1800)

        FakeRedis.values = {}
        FakeRedis.expiries = {}
        download_store = DownloadStore("redis://unused", ttl_seconds=300)
        with patch("wotbot.discovery.store.redis.from_url", return_value=FakeRedis()):
            handle = await download_store.put(
                DownloadRecord(endpoint="https://data.example/file"),
                ttl_seconds=900,
            )
            self.assertEqual((await download_store.get(handle)).filename, "download")
        self.assertEqual(next(iter(FakeRedis.expiries.values())), 300)


class AgentToolTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_tools_use_source_ids_and_runtime_thread(self) -> None:
        service = MagicMock()
        service.search_sources = AsyncMock(return_value={"items": []})
        service.discover = AsyncMock(return_value={"items": []})
        service.onboard = AsyncMock(return_value={"created": True})
        config = {"configurable": {"thread_id": "thread-a"}}
        with patch(
            "wotbot.agent.tools.external_discovery.DiscoveryService",
            return_value=service,
        ):
            await sources_search.ainvoke({"query": "Luxembourg"})
            await discover_external.ainvoke(
                {
                    "source_id": "source-a",
                    "query": "weather",
                    "limit": 4,
                },
                config=config,
            )
            await onboard_candidate.ainvoke({"candidate_id": "candidate-a"}, config=config)
        service.search_sources.assert_awaited_once_with(query="Luxembourg", limit=10)
        service.discover.assert_awaited_once_with(
            source_id="source-a",
            query="weather",
            limit=4,
            thread_id="thread-a",
        )
        service.onboard.assert_awaited_once_with(candidate_id="candidate-a", thread_id="thread-a")

    async def test_discovery_requires_a_thread(self) -> None:
        with self.assertRaisesRegex(ValueError, "conversation thread"):
            await discover_external.ainvoke(
                {
                    "source_id": "source-a",
                    "query": "roads",
                },
                config={"configurable": {}},
            )

    async def test_registration_requires_interrupt_confirmation(self) -> None:
        class State(TypedDict, total=False):
            result: dict[str, Any]

        async def register(_state: State) -> State:
            return {
                "result": await register_external_source.ainvoke({"url": "https://data.example"})
            }

        builder = StateGraph(State)
        builder.add_node("register", register)
        builder.set_entry_point("register")
        builder.add_edge("register", END)
        graph = builder.compile(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "registration-a"}}
        interrupted = await graph.ainvoke({}, config=config)
        self.assertIn("__interrupt__", interrupted)
        resumed = await graph.ainvoke(
            Command(
                resume={
                    "status": "source_registered",
                    "source_id": "urn:wotbot:source:udata:test",
                    "thing_id": "urn:wotbot:external:openapi:test",
                }
            ),
            config=config,
        )
        self.assertEqual(resumed["result"]["status"], "source_registered")
        self.assertEqual(resumed["result"]["source_id"], "urn:wotbot:source:udata:test")
        self.assertEqual(resumed["result"]["thing_id"], "urn:wotbot:external:openapi:test")

    async def test_registration_rejects_unscoped_config_and_credentialed_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "configuration requires a provider"):
            await register_external_source.ainvoke(
                {
                    "url": "https://data.example",
                    "config": {"token": "must-not-enter-the-interrupt"},
                }
            )
        with self.assertRaisesRegex(ValueError, "valid HTTP"):
            await register_external_source.ainvoke(
                {"url": "https://user:password@data.example/spec.json"}
            )

    def test_registration_tool_exposes_config_without_runnable_config_collision(self) -> None:
        schema = register_external_source.tool_call_schema
        self.assertIn("config", schema["properties"])
        self.assertNotIn("source_config", schema["properties"])


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_source_search_is_deterministic_and_hides_config(self) -> None:
        service = DiscoveryService(Settings())
        records = [
            source_record(),
            SourceRecord(
                id="source-b",
                provider="toolhive",
                external_id="http://toolhive:8080",
                title="Local MCP servers",
                description="Developer tools",
                tags=["MCP"],
                config={"url": "http://toolhive:8080"},
                network_access="private",
                security_name="source_sc",
                security_scheme="nosec",
            ),
        ]
        # Only the credential lookup touches the database; the serializer under
        # test is the real one, so config leaking into it would be caught here.
        with (
            patch.object(service, "_list_source_records", return_value=records),
            patch("wotbot.discovery.service.get_session_factory", return_value=MagicMock()),
            patch("wotbot.discovery.service.credential_schemes", return_value={}),
        ):
            result = await service.search_sources(
                query="find useful Luxembourg weather data", limit=10
            )
        self.assertEqual(result["items"][0]["source_id"], records[0].id)
        self.assertNotIn("config", json.dumps(result["items"]))
        self.assertEqual(result["items"][0]["credential_status"], "not_required")

    async def test_source_search_never_hides_the_registry_on_a_vocabulary_miss(self) -> None:
        """A source is named by scraped metadata, which rarely matches the ask.

        The Luxembourg portal registers as "Home - Portail Open Data", so a
        query naming Luxembourg scores zero against it. Filtering those out
        returned an empty list and stranded the request with nothing to search.
        """
        service = DiscoveryService(Settings())
        records = [
            SourceRecord(
                id="source-a",
                provider="udata",
                external_id="https://data.example",
                title="Home - Portail Open Data",
                description="",
                tags=[],
                config={"url": "https://data.example"},
                network_access="public",
                security_name="source_sc",
                security_scheme="nosec",
            )
        ]
        with (
            patch.object(service, "_list_source_records", return_value=records),
            patch("wotbot.discovery.service.get_session_factory", return_value=MagicMock()),
            patch("wotbot.discovery.service.credential_schemes", return_value={}),
        ):
            result = await service.search_sources(query="Luxembourg", limit=10)

        self.assertEqual([item["source_id"] for item in result["items"]], ["source-a"])

    async def test_source_search_still_ranks_a_real_match_first(self) -> None:
        service = DiscoveryService(Settings())
        records = [
            source_record(source_id="unrelated"),
            SourceRecord(
                id="matching",
                provider="dcat",
                external_id="https://swiss.example",
                title="opendata.swiss",
                description="Swiss federal open government data",
                tags=["Switzerland"],
                config={"url": "https://swiss.example"},
                network_access="public",
                security_name="source_sc",
                security_scheme="nosec",
            ),
        ]
        with (
            patch.object(service, "_list_source_records", return_value=records),
            patch("wotbot.discovery.service.get_session_factory", return_value=MagicMock()),
            patch("wotbot.discovery.service.credential_schemes", return_value={}),
        ):
            result = await service.search_sources(query="Switzerland", limit=10)

        self.assertEqual(result["items"][0]["source_id"], "matching")

    async def test_a_broken_source_record_is_not_reported_as_an_unreachable_portal(self) -> None:
        """The two need different fixes, so they must not share a status.

        A stored source that can no longer be rebuilt never reaches the
        network. Reporting it as "the external source is unavailable" sends the
        user to check a portal that is fine, and invites a retry that cannot
        succeed.
        """
        service = DiscoveryService(Settings())
        record = source_record()
        with (
            patch.object(service, "_find_source", return_value=record),
            patch.object(
                service,
                "_source_runtime",
                side_effect=SourceConfigurationError("url=https://internal.example/secret"),
            ),
        ):
            result = await service.discover(
                source_id=record.id, query="roads", limit=10, thread_id="thread-a"
            )

        self.assertEqual(result["status"], "source_misconfigured")
        self.assertIn("re-register", result["message"].casefold())
        # The underlying message names stored configuration, so it must not escape.
        self.assertNotIn("internal.example", json.dumps(result))
        self.assertNotIn("unavailable", result["message"].casefold())

    async def test_an_unreachable_portal_still_reports_as_unavailable(self) -> None:
        service = DiscoveryService(Settings())
        record = source_record()
        with (
            patch.object(service, "_find_source", return_value=record),
            patch.object(
                service,
                "_source_runtime",
                side_effect=SourceUnavailableError("portal returned HTTP 503"),
            ),
        ):
            result = await service.discover(
                source_id=record.id, query="roads", limit=10, thread_id="thread-a"
            )

        self.assertEqual(result["status"], "source_unavailable")

    async def test_discovery_stores_provider_results_in_current_thread(self) -> None:
        service = DiscoveryService(Settings())
        record = source_record()
        source = SourceDefinition(
            id=record.id,
            external_id=record.external_id,
            provider="udata",
            title=record.title,
            config={"url": "https://data.example"},
        )
        draft = CandidateDraft(
            provider="udata",
            source_id=record.id,
            external_id="roads",
            kind="dataset",
            title="Roads",
        )
        service._candidate_store.put_many = AsyncMock(return_value=["candidate-a"])
        with (
            patch.object(service, "_find_source", return_value=record),
            patch.object(service, "_source_runtime", return_value=(source, MagicMock())),
            patch.object(PROVIDERS["udata"], "search", new=AsyncMock(return_value=[draft])),
        ):
            result = await service.discover(
                source_id=record.id,
                query="roads",
                limit=10,
                thread_id="thread-a",
            )
        self.assertEqual(result["items"][0]["candidate_id"], "candidate-a")
        [stored] = service._candidate_store.put_many.await_args.args[0]
        self.assertEqual((stored.scope_kind, stored.scope_id), ("thread", "thread-a"))

    async def test_missing_and_failing_sources_return_sanitized_unavailable(self) -> None:
        service = DiscoveryService(Settings())
        with patch.object(service, "_find_source", return_value=None):
            missing = await service.discover(
                source_id="deleted",
                query="roads",
                limit=10,
                thread_id="thread-a",
            )
        self.assertEqual(missing["status"], "source_unavailable")
        self.assertNotIn("config", json.dumps(missing))

        record = source_record()
        with (
            patch.object(service, "_find_source", return_value=record),
            patch.object(
                service,
                "_source_runtime",
                side_effect=SourceUnavailableError("secret endpoint"),
            ),
        ):
            failed = await service.discover(
                source_id=record.id,
                query="roads",
                limit=10,
                thread_id="thread-a",
            )
        self.assertEqual(failed["status"], "source_unavailable")
        self.assertNotIn("secret endpoint", json.dumps(failed))

    async def test_a_defect_in_a_provider_is_not_reported_as_an_unavailable_source(self) -> None:
        """A bug must reach the error handler instead of hiding as a degraded result.

        Provider-caused failures inherit from ProviderError and become a
        sanitized ``source_unavailable``. Anything else is our defect, and
        silently reporting it as an unreachable source is what made these
        failures undiagnosable.
        """
        service = DiscoveryService(Settings())
        record = source_record()
        broken = AsyncMock(side_effect=AttributeError("'NoneType' has no attribute 'get'"))
        with (
            patch.object(service, "_find_source", return_value=record),
            patch.object(
                service,
                "_source_runtime",
                return_value=(
                    SourceDefinition(id=record.id, provider=record.provider, title=record.title),
                    None,
                ),
            ),
            patch.dict(PROVIDERS, {record.provider: MagicMock(search=broken)}),
            self.assertRaises(AttributeError),
        ):
            await service.discover(
                source_id=record.id,
                query="roads",
                limit=10,
                thread_id="thread-a",
            )

    def test_source_credentials_raise_a_source_owned_sanitized_challenge(self) -> None:
        service = DiscoveryService(Settings())
        record = source_record(scheme="apikey")
        session_factory = MagicMock()
        session_factory.return_value.__enter__.return_value = MagicMock()
        with (
            patch(
                "wotbot.discovery.service.get_session_factory",
                return_value=session_factory,
            ),
            patch("wotbot.discovery.service.get_source_credential", return_value=None),
            self.assertRaises(CredentialChallengeError) as raised,
        ):
            service._source_runtime(record)
        self.assertEqual(
            raised.exception.public(),
            {
                "status": "credential_required",
                "owner_kind": "source",
                "source_id": record.id,
                "security_name": "source_sc",
                "scheme": "apikey",
                "message": "This external source requires credentials.",
            },
        )

    def test_public_request_budget_is_provider_specific(self) -> None:
        service = DiscoveryService(Settings())
        with patch("wotbot.discovery.service.BoundedHttpClient") as client:
            _source, public_http = service._source_runtime(source_record())
            self.assertIs(public_http, client.return_value)
            client.assert_called_once_with(
                mode="public", max_requests=12, max_bytes=4 * 1024 * 1024
            )

            client.reset_mock()
            edc_record = SourceRecord(
                id="urn:wotbot:source:edc-v3:test",
                provider="edc-v3",
                external_id="https://counterparty.example.test/protocol",
                title="Test EDC",
                description="",
                tags=[],
                config={
                    "management_url": "https://management.example.test",
                    "counter_party_address": "https://counterparty.example.test/protocol",
                    "protocol": "dataspace-protocol-http",
                    "api_key_header": "X-Api-Key",
                    "poll_interval_seconds": 2,
                    "poll_timeout_seconds": 120,
                },
                network_access="public",
                security_name="source_sc",
                security_scheme="nosec",
            )
            _source, public_http = service._source_runtime(edc_record)
            self.assertIs(public_http, client.return_value)
            client.assert_called_once_with(mode="public", max_requests=128, max_bytes=1_048_576)

    async def test_udata_response_budget_accepts_large_pages_but_still_limits_reads(self) -> None:
        # Dataset listings include resource histories and other metadata that
        # can exceed the generic public probe's 1 MiB cap on real uData portals.
        for metadata_bytes, accepted in ((2 * 1024 * 1024, True), (4 * 1024 * 1024, False)):
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "roads",
                            "title": "Road network",
                            "resources": [{"id": "csv", "url": "https://data.example/roads.csv"}],
                            "extras": {"metadata": "x" * metadata_bytes},
                        }
                    ]
                }
            ).encode()
            for declared_length in (len(body), None):
                with self.subTest(metadata_bytes=metadata_bytes, declared_length=declared_length):
                    service = DiscoveryService(Settings())
                    record = source_record()
                    service._candidate_store.put_many = AsyncMock(return_value=["candidate-a"])
                    response = MagicMock(
                        status=200,
                        headers={"Content-Type": "application/json"},
                        content_length=declared_length,
                        url="https://data.example/api/1/datasets/",
                    )
                    session = MagicMock()

                    async def chunks(size):
                        for offset in range(0, len(body), size):
                            yield body[offset : offset + size]

                    response.content.iter_chunked = chunks
                    with (
                        patch.object(service, "_find_source", return_value=record),
                        patch.object(
                            BoundedHttpClient,
                            "_open",
                            new=AsyncMock(return_value=(session, response)),
                        ) as opened,
                    ):
                        result = await service.discover(
                            source_id=record.id, query="", limit=25, thread_id="thread-a"
                        )
                    attempts = 1 if accepted else 4  # Page sizes 10, 5, 2, then 1.
                    self.assertEqual(opened.await_count, attempts)
                    self.assertEqual(session.__aexit__.await_count, attempts)
                    self.assertEqual(response.__aexit__.await_count, attempts)
                    if accepted:
                        self.assertEqual(result["items"][0]["title"], "Road network")
                        service._candidate_store.put_many.assert_awaited_once()
                    else:
                        self.assertEqual(result["status"], "source_unavailable")
                        service._candidate_store.put_many.assert_not_awaited()

    async def test_onboarding_rejects_deleted_or_cross_provider_sources(self) -> None:
        service = DiscoveryService(Settings())
        candidate = CandidateRecord.from_draft(
            CandidateDraft(
                provider="udata",
                source_id="source-a",
                external_id="roads",
                kind="dataset",
                title="Roads",
            ),
            scope_kind="thread",
            scope_id="thread-a",
        )
        service._candidate_store.get = AsyncMock(return_value=candidate)
        with patch.object(service, "_find_source", return_value=None):
            missing = await service.onboard(candidate_id="candidate", thread_id="thread-a")
        self.assertEqual(missing["status"], "source_unavailable")
        with (
            patch.object(
                service,
                "_find_source",
                return_value=source_record(provider="toolhive"),
            ),
            self.assertRaisesRegex(ValueError, "different provider"),
        ):
            await service.onboard(candidate_id="candidate", thread_id="thread-a")

    async def test_onboarding_returns_service_suggestions_for_new_and_existing_datasets(
        self,
    ) -> None:
        service = DiscoveryService(Settings())
        record = source_record()
        source = SourceDefinition(
            id=record.id, provider=record.provider, title=record.title, config=record.config
        )
        suggested_url = "https://api.example/docs"
        for already_exists in (False, True):
            for resource_type in ("documentation", "main"):
                with self.subTest(already_exists=already_exists, resource_type=resource_type):
                    candidate = CandidateRecord.from_draft(
                        CandidateDraft(
                            provider="udata",
                            source_id=record.id,
                            external_id="roads",
                            kind="dataset",
                            title="Roads",
                            payload={
                                "resources": resources(
                                    [
                                        {
                                            "url": suggested_url,
                                            "title": "Road API docs",
                                            "type": resource_type,
                                        }
                                    ]
                                )
                            },
                        ),
                        scope_kind="thread",
                        scope_id="thread-a",
                    )
                    stored = thing_record()
                    # Existing Things may predate the service links in the current candidate.
                    stored = replace(stored, document={"id": stored.id, "title": "Local title"})
                    service._candidate_store.get = AsyncMock(return_value=candidate)
                    with (
                        patch.object(service, "_find_source", return_value=record),
                        patch.object(service, "_source_runtime", return_value=(source, None)),
                        patch.object(
                            service,
                            "_find_existing",
                            return_value=stored if already_exists else None,
                        ),
                        patch(
                            "wotbot.discovery.service.get_session_factory", return_value=MagicMock()
                        ),
                        patch("wotbot.discovery.service.ThingCatalogWriteService") as writer,
                        patch.object(
                            PROVIDERS["udata"],
                            "onboarding_document",
                            wraps=PROVIDERS["udata"].onboarding_document,
                        ) as generate,
                    ):
                        writer.return_value.create_discovered.return_value = (stored, True)
                        result = await service.onboard(
                            candidate_id="candidate", thread_id="thread-a"
                        )

                    self.assertEqual(result["created"], not already_exists)
                    if resource_type == "documentation":
                        self.assertEqual(
                            result["suggested_sources"],
                            [
                                {
                                    "url": suggested_url,
                                    "title": "Road API docs",
                                    "kind": "documentation",
                                }
                            ],
                        )
                    else:
                        self.assertNotIn("suggested_sources", result)
                    if already_exists:
                        generate.assert_not_awaited()
                        writer.assert_not_called()
                        self.assertEqual(stored.document, {"id": stored.id, "title": "Local title"})
                    else:
                        generate.assert_awaited_once()
                        writer.return_value.create_discovered.assert_called_once()
                        written = writer.return_value.create_discovered.call_args.args[0]
                        self.assertNotIn("suggested_sources", written)

    async def test_existing_api_things_report_newer_compiler_as_refreshable(self) -> None:
        service = DiscoveryService(Settings())
        for provider in ("openapi", "edc-v3", "tx-bootstrap"):
            for stored_version in (2, 3):
                with self.subTest(provider=provider, stored_version=stored_version):
                    record = source_record(source_id=f"source-{provider}", provider=provider)
                    source = SourceDefinition(id=record.id, provider=provider, title="API")
                    candidate = CandidateRecord.from_draft(
                        CandidateDraft(
                            provider=provider,
                            source_id=record.id,
                            external_id="api",
                            kind="api-service",
                            title="API",
                            payload={"spec_digest": "same-digest", "compiler_version": 3},
                        ),
                        scope_kind="thread",
                        scope_id="thread-a",
                    )
                    existing = thing_record(source_id=record.id, provider=provider)
                    existing.document["wotbot:generation"] = {
                        "provider": provider,
                        "specificationDigest": "same-digest",
                        "compilerVersion": stored_version,
                    }
                    with (
                        patch.object(service, "_find_existing", return_value=existing),
                        patch.object(PROVIDERS[provider], "onboarding_document") as generate,
                    ):
                        result = await service._onboard_resource(
                            source_record=record, source=source, candidate=candidate
                        )

                    self.assertFalse(result["created"])
                    self.assertEqual(result["refresh_available"], stored_version == 2)
                    self.assertNotIn("suggested_sources", result)
                    generate.assert_not_awaited()

    async def test_download_resolves_source_from_trusted_origin(self) -> None:
        service = DiscoveryService(Settings())
        thing = thing_record()
        record = source_record()
        source = SourceDefinition(
            id=record.id,
            external_id=record.external_id,
            provider="udata",
            title=record.title,
            config={"url": "https://data.example"},
        )
        service._download_store.put = AsyncMock(return_value="opaque-handle")
        acquire = AsyncMock(
            return_value=(
                DownloadRecord(endpoint="https://data.example/roads.csv", public=True),
                None,
            )
        )
        with (
            patch.object(service, "_find_thing", return_value=thing),
            patch.object(service, "_find_source", return_value=record),
            patch.object(service, "_source_runtime", return_value=(source, MagicMock())),
            patch.object(PROVIDERS["udata"], "acquire", new=acquire),
        ):
            result = await service.invoke_thing_action(
                thing_id=thing.id,
                action="download_csv_deadbeef",
                input_data=None,
            )
        self.assertEqual(result["kind"], "download")
        self.assertEqual(result["download_url"], "/api/discovery/downloads/opaque-handle")
        self.assertNotIn("data.example", json.dumps(result))

    async def test_edc_api_action_uses_protected_operation_metadata(self) -> None:
        service = DiscoveryService(Settings())
        thing_id = "urn:wotbot:external:edc-v3:orders"
        action = "getOrder"
        thing = ThingRecord(
            id=thing_id,
            title="Orders",
            description="",
            tags=[],
            origin_kind="discovery",
            origin_provider="edc-v3",
            origin_external_id="asset-1",
            origin_source_id="source-edc",
            document={
                "id": thing_id,
                "actions": {
                    action: {
                        "title": "Get order",
                        "wotbot:generatedBy": "edc-v3",
                        "forms": [
                            {
                                "href": provider_action_href(thing_id, action)
                                + "{?orderId,expand}",
                                "wotbot:providerOperation": "invoke",
                                "wotbot:httpMethod": "GET",
                                "wotbot:path": "/orders/{orderId}",
                                "wotbot:pathVariables": ["orderId"],
                                "wotbot:queryVariables": ["expand"],
                            }
                        ],
                    }
                },
            },
            document_hash="hash",
        )
        record = source_record(source_id="source-edc", provider="edc-v3")
        source = SourceDefinition(
            id=record.id,
            external_id="https://provider.example/protocol",
            provider="edc-v3",
            title=record.title,
            config={},
        )
        invoke_api = AsyncMock(
            return_value=ProviderResponse(
                body=b'{"ok":true}',
                content_type="application/json",
            )
        )
        with (
            patch.object(service, "_find_thing", return_value=thing),
            patch.object(service, "_find_source", return_value=record),
            patch.object(service, "_source_runtime", return_value=(source, MagicMock())),
            patch.object(PROVIDERS["edc-v3"], "invoke_api", new=invoke_api),
        ):
            result = await service.invoke_thing_action(
                thing_id=thing.id,
                action=action,
                input_data=None,
                uri_variables={"orderId": "A/B", "expand": True},
            )

        invoke_api.assert_awaited_once_with(
            source,
            external_id="asset-1",
            method="GET",
            path="/orders/{orderId}",
            path_variables=("orderId",),
            query_variables=("expand",),
            uri_variables={"orderId": "A/B", "expand": True},
            input_data=None,
            public_http=ANY,
        )
        self.assertEqual(
            result,
            {
                "kind": "response",
                "content_type": "application/json",
                "body_base64": "eyJvayI6dHJ1ZX0=",
            },
        )


class ProviderTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_toolhive_rewrites_only_the_reported_host(self) -> None:
        source = SourceDefinition(
            id="source-a",
            provider="toolhive",
            title="Local",
            network_access="private",
            config={
                "url": "http://toolhive:8080",
                "reported_host": "127.0.0.1",
                "runtime_host": "host.docker.internal",
            },
        )
        with patch(
            "wotbot.discovery.providers.toolhive.source_json",
            new=AsyncMock(
                return_value={
                    "workloads": [
                        {
                            "name": "files",
                            "status": "running",
                            "url": "http://127.0.0.1:41100/mcp",
                        },
                        {
                            "name": "stopped",
                            "status": "stopped",
                            "url": "http://127.0.0.1:41101/mcp",
                        },
                    ]
                }
            ),
        ):
            results = await ToolHiveProvider().search(
                source,
                prepare_search_intent("everything", source),
                10,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].payload["endpoint"],
            "mcp+http://host.docker.internal:41100/mcp",
        )

    def test_public_toolhive_cannot_pivot_to_an_unrelated_runtime_host(self) -> None:
        source = SourceDefinition(
            id="source-a",
            provider="toolhive",
            title="Public ToolHive",
            network_access="public",
            config={
                "url": "https://registry.example.test",
                "reported_host": "127.0.0.1",
                "runtime_host": "169.254.169.254",
            },
        )
        with self.assertRaisesRegex(SourceProtocolError, "must match its registry host"):
            ToolHiveProvider.runtime_endpoint(source, "http://127.0.0.1:41100/mcp")

    def test_toolhive_rejects_an_endpoint_from_an_unrelated_reported_host(self) -> None:
        source = SourceDefinition(
            id="source-a",
            provider="toolhive",
            title="Local",
            network_access="private",
            config={
                "url": "http://toolhive:8080",
                "reported_host": "127.0.0.1",
                "runtime_host": "host.docker.internal",
            },
        )
        with self.assertRaisesRegex(SourceProtocolError, "configured reported host"):
            ToolHiveProvider.runtime_endpoint(source, "http://169.254.169.254/mcp")

    def test_dataset_td_has_descriptive_distribution_actions_without_source_link(self) -> None:
        candidate = CandidateDraft(
            provider="udata",
            source_id="source-a",
            external_id="roads",
            kind="dataset",
            title="Roads",
            summary="Road network",
        )
        document = dataset_document(
            candidate,
            [
                {
                    "id": "csv",
                    "title": "Road network CSV",
                    "description": "Latest road geometry",
                    "format": "CSV",
                    "media_type": "text/csv",
                }
            ],
        )
        action = next(iter(document["actions"].values()))
        self.assertEqual(action["title"], "Download Road network CSV")
        self.assertEqual(action["wotbot:format"], "CSV")
        self.assertNotIn("wotbot:source", json.dumps(document))

    def test_provider_schemas_and_credential_headers_remain_secret_free(self) -> None:
        self.assertEqual(
            set(PROVIDERS),
            {"udata", "dcat", "toolhive", "edc-v3", "tx-bootstrap", "openapi"},
        )
        schema = PROVIDERS["edc-v3"].registration_schema()
        self.assertEqual(schema["default_security_scheme"], "apikey")
        self.assertEqual(
            PROVIDERS["tx-bootstrap"].registration_schema()["default_security_scheme"],
            "bearer",
        )
        self.assertNotIn("secret", json.dumps(schema).casefold())
        source = SourceDefinition(
            id="source",
            provider="udata",
            title="Source",
            security_scheme="apikey",
            config={"api_key_header": "X-Catalog-Key"},
            credential={"apiKey": "secret"},
        )
        self.assertEqual(credential_headers(source), {"X-Catalog-Key": "secret"})

    async def test_udata_query_preparation_keeps_concise_provider_terms(self) -> None:
        source = SourceDefinition(
            id="source-a",
            provider="udata",
            title="Luxembourg open data",
            tags=("Luxembourg",),
        )
        intent = prepare_search_intent(
            "MeteoLux weather observations and forecasts Luxembourg temperature precipitation",
            source,
        )
        self.assertNotIn("Luxembourg", intent.terms)
        self.assertIn("MeteoLux", intent.terms)


class DownloadRouteTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_download_proxy_forwards_ranges_and_streams_without_buffering(self) -> None:
        class Content:
            async def iter_chunked(self, _size: int):
                yield b"part-one"
                yield b"-part-two"

        upstream = MagicMock()
        upstream.status = 206
        upstream.headers = {
            "Content-Type": "text/csv",
            "Content-Range": "bytes 0-16/17",
            "Accept-Ranges": "bytes",
        }
        upstream.content = Content()
        session = MagicMock()
        session.close = AsyncMock()
        request = MagicMock()
        request.headers = {"Range": "bytes=0-16"}
        request.app.state.settings = Settings()
        record = DownloadRecord(
            endpoint="https://data.example/file.csv",
            public=True,
            content_type="text/csv",
            filename="file.csv",
        )
        with (
            patch(
                "wotbot.discovery.routes.DownloadStore.get",
                new=AsyncMock(return_value=record),
            ),
            patch(
                "wotbot.discovery.routes.BoundedHttpClient.stream",
                new=AsyncMock(return_value=(session, upstream)),
            ) as opened,
        ):
            response = await _download_response("a" * 43, request)
            body = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(body, b"part-one-part-two")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-16/17")
        self.assertEqual(opened.await_args.kwargs["headers"]["Range"], "bytes=0-16")
        session.close.assert_awaited_once()

    def test_registration_challenge_is_http_428_and_secret_free(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _registration_result(
                {
                    "credential_challenge": {
                        "status": "credential_required",
                        "owner_kind": "source",
                        "source_id": "source-a",
                        "security_name": "source_sc",
                        "scheme": "apikey",
                        "token": "must-not-escape",
                    }
                }
            )
        self.assertEqual(raised.exception.status_code, 428)
        self.assertNotIn("must-not-escape", json.dumps(raised.exception.detail))

    async def test_authenticated_trusted_download_rejects_cross_origin_redirect(self) -> None:
        """A credential must never follow a redirect to another origin.

        The five hand-rolled redirect loops were consolidated into
        BoundedHttpClient, so this now pins the shared guard rather than one
        copy of it.
        """
        response = MagicMock()
        response.status = 302
        response.headers = {"Location": "https://attacker.example/file"}
        response.release = MagicMock()
        session = MagicMock()
        session.request = AsyncMock(return_value=response)
        session.close = AsyncMock()
        client = BoundedHttpClient(mode="trusted", max_requests=None)
        with (
            patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session),
            self.assertRaisesRegex(UnsafeUrlError, "cannot redirect across origins"),
        ):
            await client.stream(
                "https://provider.example/file",
                headers={"Authorization": "secret"},
                credentialed=True,
            )
        session.close.assert_awaited()

    async def test_an_unauthenticated_download_may_follow_a_redirect(self) -> None:
        """Without a credential there is nothing to leak, so the hop is allowed.

        An Accept header is not a secret. Treating every header as one refused
        plain http-to-https upgrades and the bare-to-www redirects real portals
        serve, which is why callers now say whether they carry a credential.
        """
        redirect = MagicMock()
        redirect.status = 302
        redirect.headers = {"Location": "https://cdn.example/file"}
        redirect.release = MagicMock()
        final = MagicMock()
        final.status = 200
        final.headers = {}
        session = MagicMock()
        session.request = AsyncMock(side_effect=[redirect, final])
        session.close = AsyncMock()
        client = BoundedHttpClient(mode="trusted", max_requests=None)
        with patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session):
            _session, response = await client.stream(
                "https://provider.example/file",
                headers={"Accept": "text/csv"},
            )
        self.assertEqual(response.status, 200)


class PromptBoundaryTestCase(unittest.TestCase):
    def test_prompt_uses_source_registry_and_contains_no_provider_workflow(self) -> None:
        self.assertIn("sources_search", DISCOVERY_PROMPT)
        self.assertIn("discover_external", DISCOVERY_PROMPT)
        self.assertNotIn("wotbot:DiscoverySource", DISCOVERY_PROMPT)
        self.assertNotIn("Source Thing", DISCOVERY_PROMPT)
        for term in ("ToolHive", "EDC", "reported_host", "runtime_host"):
            self.assertNotIn(term, DISCOVERY_PROMPT)

    def test_gateway_seed_is_absent(self) -> None:
        repository = Path(__file__).resolve().parents[3]
        self.assertFalse((repository / "packages" / "thing-seeds").exists())
        self.assertNotIn(
            "seed-things", (repository / "apps" / "wotbot" / "pyproject.toml").read_text()
        )
