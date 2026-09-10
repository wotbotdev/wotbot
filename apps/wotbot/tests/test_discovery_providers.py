"""Characterization tests for the EDC, uData, and DCAT providers.

These three parse content that an external source fully controls, and they had
no direct coverage. The tests below pin the parsing and safety behaviour that
the HTTP-layer consolidation must preserve, so they are deliberately written
against each provider's public surface rather than its internals.
"""

from __future__ import annotations

import json
import pathlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from wotbot.discovery.errors import (
    SourceProtocolError,
    SourceResponseTooLargeError,
    SourceUnavailableError,
    UnsafeUrlError,
)
from wotbot.discovery.http import BoundedHttpClient, HttpPayload
from wotbot.discovery.models import CandidateDraft, CandidateRecord, SearchIntent, SourceDefinition
from wotbot.discovery.providers import PROVIDERS
from wotbot.discovery.providers import public as public_module
from wotbot.discovery.providers.dcat import DcatProvider, graph_resources
from wotbot.discovery.providers.edc_v3 import (
    EdcV3Provider,
    edr_ttl,
    response_id,
    secret_headers,
)
from wotbot.discovery.providers.public import detectors, resolve_public_source
from wotbot.discovery.providers.udata import (
    UdataProvider,
    resources,
    service_suggestions,
    udata_queries,
)
from wotbot.discovery.search import prepare_search_intent
from wotbot.discovery.service import _management_sources
from wotbot.discovery.source_models import SourceRecord
from wotbot.discovery.store import CandidateStore, DownloadStore, reset_clients

TURTLE_CATALOG = """
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct: <http://purl.org/dc/terms/> .

<https://data.example/dataset/roads> a dcat:Dataset ;
    dct:title "Road network" ;
    dct:description "Every classified road" ;
    dcat:keyword "roads", "transport" ;
    dcat:distribution <https://data.example/dist/roads.csv> .

<https://data.example/dist/roads.csv> a dcat:Distribution ;
    dct:title "Roads CSV" ;
    dct:format "CSV" ;
    dcat:mediaType "text/csv" ;
    dcat:byteSize "20480" ;
    dcat:downloadURL <https://data.example/files/roads.csv> .
"""


def edc_source(**overrides: object) -> SourceDefinition:
    config = {
        "management_url": "https://edc.example/management",
        "counter_party_address": "https://provider.example/protocol",
        "protocol": "dataspace-protocol-http",
        "poll_interval_seconds": 0,
        "poll_timeout_seconds": 5,
        **overrides,
    }
    return SourceDefinition(
        id="urn:wotbot:source:edc-v3:test",
        provider="edc-v3",
        title="Partner dataspace",
        external_id="https://provider.example/protocol",
        config=config,
        security_scheme="apikey",
        credential={"apiKey": "secret"},
    )


def udata_source() -> SourceDefinition:
    return SourceDefinition(
        id="urn:wotbot:source:udata:test",
        provider="udata",
        title="Open data portal",
        external_id="https://data.example",
        config={"url": "https://data.example"},
    )


def dcat_source() -> SourceDefinition:
    return SourceDefinition(
        id="urn:wotbot:source:dcat:test",
        provider="dcat",
        title="DCAT catalog",
        external_id="https://data.example/catalog.ttl",
        config={"url": "https://data.example/catalog.ttl"},
    )


class EdcSecretHeaderTestCase(unittest.TestCase):
    def test_a_normal_endpoint_data_reference_becomes_one_header(self) -> None:
        self.assertEqual(
            secret_headers({"authKey": "Authorization", "authCode": "Bearer token"}),
            {"Authorization": "Bearer token"},
        )

    def test_a_missing_auth_key_defaults_to_authorization(self) -> None:
        self.assertEqual(secret_headers({"authCode": "token"}), {"Authorization": "token"})

    def test_hop_by_hop_and_framing_headers_are_refused(self) -> None:
        for name in ("Host", "content-length", "Connection", "Transfer-Encoding"):
            with self.subTest(header=name):
                with self.assertRaisesRegex(SourceProtocolError, "unsafe authorization header"):
                    secret_headers({"authKey": name, "authCode": "token"})

    def test_a_header_name_with_separators_is_refused(self) -> None:
        for name in ("X-Api Key", "X-Api:Key", "X-Api\nInjected", "X-Api\r\nSet-Cookie"):
            with self.subTest(header=name):
                with self.assertRaisesRegex(SourceProtocolError, "unsafe authorization header"):
                    secret_headers({"authKey": name, "authCode": "token"})

    def test_an_incomplete_reference_is_refused(self) -> None:
        with self.assertRaises(SourceProtocolError):
            secret_headers({"authKey": "Authorization"})
        with self.assertRaises(SourceProtocolError):
            secret_headers("not-a-mapping")


class EdcTtlTestCase(unittest.TestCase):
    def test_expires_in_is_used_directly(self) -> None:
        self.assertEqual(edr_ttl({"expiresIn": 300}), 300)
        self.assertEqual(edr_ttl({"expiresIn": "120"}), 120)

    def test_an_absent_expiry_leaves_the_ttl_to_the_caller(self) -> None:
        self.assertIsNone(edr_ttl({}))

    def test_epoch_seconds_and_milliseconds_are_both_understood(self) -> None:
        # The millisecond heuristic triggers above 10^10, so both cases need
        # realistic epochs rather than small synthetic ones.
        self.assertEqual(edr_ttl({"expiresAt": 1_800_000_600}, now=1_800_000_000), 600)
        self.assertEqual(edr_ttl({"expiresAt": 1_800_000_600_000}, now=1_800_000_000), 600)

    def test_an_iso_timestamp_is_understood(self) -> None:
        ttl = edr_ttl(
            {"expiresAt": "2030-01-01T00:10:00+00:00"},
            now=1_893_456_000.0,  # 2030-01-01T00:00:00Z
        )
        self.assertEqual(ttl, 600)

    def test_an_already_expired_reference_is_refused(self) -> None:
        for address in ({"expiresIn": 0}, {"expiresIn": -1}, {"expiresAt": 900}):
            with self.subTest(address=address):
                with self.assertRaisesRegex(SourceProtocolError, "expired"):
                    edr_ttl(address, now=1_000_000)

    def test_an_unparseable_expiry_is_refused_rather_than_ignored(self) -> None:
        for address in ({"expiresIn": "soon"}, {"expiresAt": "not-a-date"}):
            with self.subTest(address=address), self.assertRaises(SourceProtocolError):
                edr_ttl(address, now=1_000_000)

    def test_response_id_accepts_both_json_ld_and_plain_identifiers(self) -> None:
        self.assertEqual(response_id({"@id": "abc"}, "id"), "abc")
        self.assertEqual(response_id({"id": "def"}, "id"), "def")
        with self.assertRaises(SourceProtocolError):
            response_id({}, "id")
        with self.assertRaises(SourceProtocolError):
            response_id(["not-an-object"], "id")


class EdcAcquireTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _catalog(with_policy: bool = True) -> dict[str, object]:
        dataset: dict[str, object] = {
            "@id": "asset-1",
            "dct:title": "Partner asset",
            "dct:description": "Shared telemetry",
        }
        if with_policy:
            dataset["odrl:hasPolicy"] = {"@id": "policy-1", "@type": "Offer"}
        return {"dcat:dataset": [dataset]}

    async def test_a_full_negotiation_produces_a_credentialled_download(self) -> None:
        responses = [
            self._catalog(),
            {"@id": "negotiation-1"},
            {"state": "FINALIZED", "contractAgreementId": "agreement-1"},
            {"@id": "transfer-1"},
            {"state": "STARTED"},
            {
                "endpoint": "https://provider.example/data/asset-1",
                "authKey": "Authorization",
                "authCode": "Bearer edr-token",
                "expiresIn": 300,
            },
        ]
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(side_effect=responses),
        ):
            download, ttl = await EdcV3Provider().acquire(
                edc_source(),
                external_id="asset-1",
                title="Partner asset",
                resource_id=None,
            )
        self.assertEqual(download.endpoint, "https://provider.example/data/asset-1")
        self.assertEqual(download.headers, {"Authorization": "Bearer edr-token"})
        self.assertEqual(download.filename, "Partner-asset")
        self.assertEqual(ttl, 300)

    async def test_an_asset_that_left_the_catalog_is_reported_as_a_source_problem(self) -> None:
        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(return_value={"dcat:dataset": []}),
            ),
            self.assertRaisesRegex(SourceProtocolError, "no longer offered"),
        ):
            await EdcV3Provider().acquire(
                edc_source(), external_id="asset-1", title="t", resource_id=None
            )

    async def test_an_asset_without_a_contract_offer_is_refused(self) -> None:
        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(return_value=self._catalog(with_policy=False)),
            ),
            self.assertRaisesRegex(SourceProtocolError, "no current contract offer"),
        ):
            await EdcV3Provider().acquire(
                edc_source(), external_id="asset-1", title="t", resource_id=None
            )

    async def test_a_negotiation_without_an_agreement_id_is_refused(self) -> None:
        responses = [self._catalog(), {"@id": "negotiation-1"}, {"state": "FINALIZED"}]
        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(side_effect=responses),
            ),
            self.assertRaisesRegex(SourceProtocolError, "without a contract agreement"),
        ):
            await EdcV3Provider().acquire(
                edc_source(), external_id="asset-1", title="t", resource_id=None
            )

    async def test_a_terminated_negotiation_stops_the_transfer(self) -> None:
        responses = [self._catalog(), {"@id": "negotiation-1"}, {"state": "TERMINATED"}]
        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(side_effect=responses),
            ),
            self.assertRaisesRegex(SourceProtocolError, "TERMINATED"),
        ):
            await EdcV3Provider().acquire(
                edc_source(), external_id="asset-1", title="t", resource_id=None
            )

    async def test_a_never_settling_negotiation_times_out(self) -> None:
        responses = [self._catalog(), {"@id": "negotiation-1"}]
        calls = {"n": 0}

        async def source_json(*_args: object, **_kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] <= len(responses):
                return responses[calls["n"] - 1]
            return {"state": "REQUESTED"}

        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(side_effect=source_json),
            ),
            self.assertRaisesRegex(SourceUnavailableError, "timed out"),
        ):
            await EdcV3Provider().acquire(
                edc_source(poll_timeout_seconds=0.05),
                external_id="asset-1",
                title="t",
                resource_id=None,
            )

    async def test_an_endpoint_that_is_not_http_is_refused(self) -> None:
        responses = [
            self._catalog(),
            {"@id": "negotiation-1"},
            {"state": "FINALIZED", "contractAgreementId": "agreement-1"},
            {"@id": "transfer-1"},
            {"state": "STARTED"},
            {"endpoint": "file:///etc/passwd", "authCode": "token"},
        ]
        with (
            patch(
                "wotbot.discovery.providers.edc_v3.source_json",
                new=AsyncMock(side_effect=responses),
            ),
            self.assertRaisesRegex(SourceProtocolError, "invalid endpoint"),
        ):
            await EdcV3Provider().acquire(
                edc_source(), external_id="asset-1", title="t", resource_id=None
            )

    async def test_search_skips_assets_that_carry_no_policy(self) -> None:
        catalog = {
            "dcat:dataset": [
                {"@id": "offered", "dct:title": "Telemetry feed", "odrl:hasPolicy": {"@id": "p"}},
                {"@id": "unoffered", "dct:title": "Telemetry archive"},
            ]
        }
        source = edc_source()
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog),
        ):
            results = await EdcV3Provider().search(
                source, prepare_search_intent("telemetry", source), 10
            )
        self.assertEqual([item.external_id for item in results], ["offered"])


class RefreshHookTestCase(unittest.TestCase):
    """The refresh merge must follow the provider's marker, not a hardcoded name."""

    def test_each_provider_preserves_only_what_another_provider_did_not_generate(self) -> None:
        current = {
            "id": "urn:wotbot:external:demo:1",
            "title": "Kept title",
            "actions": {
                "hand_written": {"title": "Manual"},
                "generated_by_udata": {"title": "Old", "wotbot:generatedBy": "udata"},
            },
        }
        generated = {
            "id": "ignored",
            "title": "Regenerated title",
            "actions": {"generated_by_udata": {"title": "New", "wotbot:generatedBy": "udata"}},
        }
        merged, removed = UdataProvider().merge_refresh(current, generated)

        self.assertEqual(merged["id"], current["id"])
        self.assertEqual(merged["title"], "Kept title")
        self.assertEqual(merged["actions"]["hand_written"], {"title": "Manual"})
        self.assertEqual(merged["actions"]["generated_by_udata"]["title"], "New")
        self.assertEqual(removed, ())

    def test_a_provider_does_not_claim_another_providers_generated_nodes(self) -> None:
        current = {
            "id": "urn:wotbot:external:demo:1",
            "actions": {"from_openapi": {"title": "Old", "wotbot:generatedBy": "openapi"}},
        }
        generated = {"id": "ignored", "actions": {}}

        # DCAT must treat an OpenAPI-generated action as somebody else's manual
        # content and keep it, rather than silently dropping it.
        merged, _removed = DcatProvider().merge_refresh(current, generated)
        self.assertIn("from_openapi", merged["actions"])

    def test_the_marker_defaults_to_the_provider_name(self) -> None:
        self.assertEqual(UdataProvider().generation_marker, "udata")
        self.assertEqual(DcatProvider().generation_marker, "dcat")
        self.assertEqual(EdcV3Provider().generation_marker, "edc-v3")


class EdcTractusXTestCase(unittest.IsolatedAsyncioTestCase):
    """Shapes a Tractus-X connector produces that a plain EDC does not.

    Verified against tx-bootstrap's e2e-runtime.sh, which is the reference for
    the request bodies and response fields a Tractus-X dataspace expects.
    """

    async def test_the_counterparty_id_is_sent_on_every_request(self) -> None:
        """Tractus-X rejects a request that does not name the provider's DID."""
        source = edc_source(counter_party_id="did:web:provider.example")
        responses = [
            {
                "dcat:dataset": [
                    {"@id": "asset-1", "dct:title": "T", "odrl:hasPolicy": {"@id": "p"}}
                ]
            },
            {"@id": "negotiation-1"},
            {"state": "FINALIZED", "contractAgreementId": "agreement-1"},
            {"@id": "transfer-1"},
            {"state": "STARTED"},
            {"endpoint": "https://provider.example/data", "authorization": "Bearer edr"},
        ]
        sender = AsyncMock(side_effect=responses)
        with patch("wotbot.discovery.providers.edc_v3.source_json", new=sender):
            await EdcV3Provider().acquire(
                source, external_id="asset-1", title="T", resource_id=None
            )

        bodies = [call.kwargs.get("body") for call in sender.await_args_list]
        posted = [body for body in bodies if body]
        self.assertEqual(len(posted), 3, "catalog, negotiation and transfer are all posts")
        for body in posted:
            self.assertEqual(body["counterPartyId"], "did:web:provider.example")

    async def test_the_counterparty_id_is_omitted_when_unset(self) -> None:
        """A plain EDC has no DID to name, and must not receive an empty one."""
        sender = AsyncMock(return_value={"dcat:dataset": []})
        with patch("wotbot.discovery.providers.edc_v3.source_json", new=sender):
            await EdcV3Provider().search(edc_source(), prepare_search_intent("", edc_source()), 5)
        body = sender.await_args.kwargs["body"]
        self.assertNotIn("counterPartyId", body)

    async def test_a_catalog_returned_as_a_list_is_read(self) -> None:
        """Tractus-X answers with an array of catalogs, one per counterparty."""
        catalog = [
            {
                "dcat:dataset": [
                    {"@id": "asset-1", "dct:title": "Telemetry", "odrl:hasPolicy": {"@id": "p"}}
                ]
            }
        ]
        source = edc_source()
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog),
        ):
            results = await EdcV3Provider().search(source, prepare_search_intent("", source), 5)
        self.assertEqual([item.external_id for item in results], ["asset-1"])

    async def test_a_single_inlined_dataset_is_read(self) -> None:
        catalog = {
            "dcat:dataset": {"@id": "only", "dct:title": "T", "odrl:hasPolicy": {"@id": "p"}}
        }
        source = edc_source()
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog),
        ):
            results = await EdcV3Provider().search(source, prepare_search_intent("", source), 5)
        self.assertEqual([item.external_id for item in results], ["only"])

    async def test_edc_prefixed_state_and_agreement_are_understood(self) -> None:
        """Whether a response is compacted or expanded varies by distribution."""
        responses = [
            {"edc:dataset": [{"@id": "a", "edc:title": "T", "odrl:hasPolicy": {"@id": "p"}}]},
            {"@id": "negotiation-1"},
            {"edc:state": "FINALIZED", "edc:contractAgreementId": "agreement-1"},
            {"@id": "transfer-1"},
            {"edc:state": "STARTED"},
            {"edc:baseUrl": "https://provider.example/data", "edc:authorization": "Bearer edr"},
        ]
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(side_effect=responses),
        ):
            download, _ttl = await EdcV3Provider().acquire(
                edc_source(), external_id="a", title="T", resource_id=None
            )
        self.assertEqual(download.endpoint, "https://provider.example/data")
        self.assertEqual(download.headers, {"Authorization": "Bearer edr"})

    def test_the_tractus_x_edr_puts_the_token_in_authorization(self) -> None:
        self.assertEqual(
            secret_headers({"authorization": "Bearer edr-token"}),
            {"Authorization": "Bearer edr-token"},
        )

    def test_an_auth_key_holding_a_token_is_not_read_as_a_header_name(self) -> None:
        self.assertEqual(
            secret_headers({"authKey": "Bearer edr-token"}),
            {"Authorization": "Bearer edr-token"},
        )

    def test_an_auth_key_holding_only_a_header_name_is_still_incomplete(self) -> None:
        for name in ("Authorization", "x-api-key"):
            with (
                self.subTest(name=name),
                self.assertRaises(SourceProtocolError),
            ):
                secret_headers({"authKey": name})


class UdataParsingTestCase(unittest.TestCase):
    def test_resource_descriptors_carry_the_metadata_the_td_action_needs(self) -> None:
        [descriptor] = resources(
            [
                {
                    "id": "res-1",
                    "title": "Roads CSV",
                    "description": "Latest geometry",
                    "url": "https://data.example/files/roads.csv",
                    "format": "csv",
                    "mime": "text/csv",
                    "filesize": 2048,
                    "last_modified": "2026-01-01",
                }
            ]
        )
        self.assertEqual(descriptor["id"], "res-1")
        self.assertEqual(descriptor["media_type"], "text/csv")
        self.assertEqual(descriptor["filename"], "roads.csv")
        self.assertEqual(descriptor["size_bytes"], 2048)

    def test_service_endpoints_are_reported_for_source_detection(self) -> None:
        """uData types every service endpoint ``api``, whatever it speaks.

        The dataset says a service exists; detection decides whether anything
        supports it, so the suggestion carries the URL rather than a protocol.
        """

        descriptors = resources(
            [
                {"id": "csv", "url": "https://data.example/roads.csv", "format": "csv"},
                {
                    "id": "svc",
                    "title": "Weather API",
                    "url": "https://api.example/v1",
                    "format": "json",
                    "type": "api",
                },
                {
                    "id": "doc",
                    "title": "API docs",
                    "url": "https://api.example/v1/docs",
                    "format": "html",
                    "type": "documentation",
                },
                {"id": "wms", "url": "https://maps.example/wms", "format": "wms", "type": "api"},
            ]
        )
        suggestions = service_suggestions(descriptors)
        self.assertEqual(
            [item["url"] for item in suggestions],
            [
                "https://api.example/v1",
                "https://api.example/v1/docs",
                "https://maps.example/wms",
            ],
        )
        self.assertEqual(suggestions[0]["title"], "Weather API")
        self.assertEqual(suggestions[2]["format"], "wms")

    def test_a_dataset_of_plain_downloads_suggests_nothing(self) -> None:
        descriptors = resources([{"id": "csv", "url": "https://data.example/a.csv"}])
        self.assertEqual(service_suggestions(descriptors), ())

    def test_a_media_type_is_inferred_from_a_bare_format(self) -> None:
        [descriptor] = resources([{"url": "https://data.example/a.json", "format": "json"}])
        self.assertEqual(descriptor["media_type"], "application/json")

    def test_resources_that_are_not_public_http_urls_are_dropped(self) -> None:
        self.assertEqual(
            resources(
                [
                    {"url": "ftp://data.example/a.csv"},
                    {"url": "https://user:pass@data.example/b.csv"},
                    {"url": ""},
                    {"not": "a resource"},
                ]
            ),
            [],
        )

    def test_a_boolean_filesize_is_not_mistaken_for_a_byte_count(self) -> None:
        [descriptor] = resources([{"url": "https://data.example/a.csv", "filesize": True}])
        self.assertNotIn("size_bytes", descriptor)

    def test_intent_is_compiled_into_bounded_full_text_probes(self) -> None:
        source = udata_source()
        queries = udata_queries(
            prepare_search_intent("WeatherService weather observations", source)
        )
        self.assertLessEqual(len(queries), 3)
        self.assertTrue(any("WeatherService" in query for query in queries))

    def test_an_empty_intent_browses_rather_than_searching(self) -> None:
        self.assertEqual(udata_queries(SearchIntent(original="")), ("",))


class UdataProviderTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_pages_shrink_without_skipping_or_repeating_candidates(self) -> None:
        source = udata_source()
        datasets = [{"id": str(i), "title": f"Dataset {i}"} for i in range(13)]
        requested: list[tuple[int, int]] = []

        async def fake_json(_self: object, _source: object, url: str, **_kw: object) -> object:
            self.assertEqual(urlparse(url).netloc, "data.example")
            params = parse_qs(urlparse(url).query)
            page_size = int(params["page_size"][0])
            page = int(params["page"][0])
            requested.append((page, page_size))
            offset = (page - 1) * page_size
            page_data = datasets[offset : offset + page_size]
            # Four adjacent datasets have large resource histories. The first
            # five fit, but the next five require a smaller page size.
            if sum(1 for item in page_data if 5 <= int(item["id"]) <= 8) > 3:
                raise SourceResponseTooLargeError("Source response is too large")
            return {
                "data": page_data,
                "next_page": "https://other.example/ignored" if offset + page_size < 13 else None,
            }

        with patch.object(UdataProvider, "_json", new=fake_json):
            results = await UdataProvider().search(source, prepare_search_intent("", source), 25)

        self.assertCountEqual([item.external_id for item in results], map(str, range(13)))
        self.assertIn((2, 5), requested)
        # Offset 5 does not align with pages of two: revisit offset 4, then deduplicate.
        self.assertIn((3, 2), requested)
        self.assertLessEqual(len(requested), 10)

    async def test_repeated_pages_cannot_exceed_the_search_request_budget(self) -> None:
        source = udata_source()
        response = {
            "data": [{"id": "roads", "title": "Roads"}],
            "next_page": "https://data.example/always-more",
        }
        with patch.object(UdataProvider, "_json", new=AsyncMock(return_value=response)) as fetch:
            results = await UdataProvider().search(source, prepare_search_intent("", source), 25)

        self.assertEqual([item.external_id for item in results], ["roads"])
        self.assertEqual(fetch.await_count, 10)

    async def test_other_protocol_errors_are_not_retried_as_oversized_pages(self) -> None:
        source = udata_source()
        with patch.object(
            UdataProvider, "_json", new=AsyncMock(side_effect=SourceProtocolError("invalid JSON"))
        ) as fetch:
            with self.assertRaisesRegex(SourceProtocolError, "invalid JSON"):
                await UdataProvider().search(source, prepare_search_intent("", source), 25)
        fetch.assert_awaited_once()

    async def test_the_query_reaches_the_backend_instead_of_being_filtered_locally(self) -> None:
        source = udata_source()
        payload = {
            "data": [
                {
                    "id": "roads",
                    "title": "Road network",
                    "description": "Every classified road",
                    "resources": [
                        {"id": "csv", "url": "https://data.example/roads.csv", "format": "csv"}
                    ],
                }
            ]
        }
        requested: list[str] = []

        async def fake_json(_self: object, _source: object, url: str, **_kw: object) -> object:
            requested.append(url)
            return payload

        with patch.object(UdataProvider, "_json", new=fake_json):
            results = await UdataProvider().search(
                source, prepare_search_intent("road network", source), 5
            )
        self.assertTrue(requested)
        self.assertIn("/api/1/datasets/", requested[0])
        self.assertIn("q=", requested[0])
        self.assertEqual([item.external_id for item in results], ["roads"])

    async def test_a_listing_that_is_not_a_dataset_page_is_a_protocol_error(self) -> None:
        source = udata_source()

        async def fake_json(_self: object, _source: object, _url: str, **_kw: object) -> object:
            return {"unexpected": "shape"}

        with patch.object(UdataProvider, "_json", new=fake_json):
            with self.assertRaises(SourceProtocolError):
                await UdataProvider().search(source, prepare_search_intent("roads", source), 5)

    async def test_a_dataset_that_only_points_at_a_service_suggests_registering_it(
        self,
    ) -> None:
        """An API entry with nothing downloadable in it.

        Such a dataset used to onboard as a Thing with no affordances at all.
        It still has none, because the dataset genuinely holds no data, but it
        now reports where the data actually lives.
        """

        descriptors = resources(
            [
                {
                    "id": "svc",
                    "title": "Meteo API endpoint",
                    "url": "https://api.example/v1",
                    "format": "json",
                    "type": "api",
                },
                {
                    "id": "doc",
                    "title": "Meteo API docs",
                    "url": "https://api.example/v1/docs",
                    "format": "html",
                    "type": "documentation",
                },
            ]
        )
        candidate = CandidateDraft(
            provider="udata",
            source_id="source-1",
            external_id="meteo",
            kind="dataset",
            title="Meteo API",
            summary="Weather readings",
            payload={"resources": descriptors},
        )
        result = await UdataProvider().onboarding_document(
            udata_source(), candidate, runtime=MagicMock()
        )
        self.assertNotIn("actions", result.document)
        self.assertEqual(
            [item["url"] for item in result.suggested_sources],
            ["https://api.example/v1", "https://api.example/v1/docs"],
        )

    async def test_a_dataset_with_downloads_onboards_without_suggestions(self) -> None:
        descriptors = resources([{"id": "csv", "url": "https://data.example/roads.csv"}])
        candidate = CandidateDraft(
            provider="udata",
            source_id="source-1",
            external_id="roads",
            kind="dataset",
            title="Roads",
            payload={"resources": descriptors},
        )
        result = await UdataProvider().onboarding_document(
            udata_source(), candidate, runtime=MagicMock()
        )
        self.assertEqual(len(result.document["actions"]), 1)
        self.assertEqual(result.suggested_sources, ())

    async def test_a_documentation_resource_cannot_be_downloaded(self) -> None:
        source = udata_source()
        dataset = {
            "id": "roads",
            "resources": [
                {
                    "id": "doc",
                    "url": "https://data.example/docs",
                    "format": "html",
                    "type": "documentation",
                }
            ],
        }

        async def fake_json(_self: object, _source: object, _url: str, **_kw: object) -> object:
            return dataset

        with patch.object(UdataProvider, "_json", new=fake_json):
            with self.assertRaisesRegex(SourceProtocolError, "link and cannot be downloaded"):
                await UdataProvider().acquire(
                    source, external_id="roads", title="Roads", resource_id="doc"
                )

    async def test_a_resource_that_vanished_is_reported_as_a_source_problem(self) -> None:
        source = udata_source()

        async def fake_json(_self: object, _source: object, _url: str, **_kw: object) -> object:
            return {"id": "roads", "resources": []}

        with patch.object(UdataProvider, "_json", new=fake_json):
            with self.assertRaisesRegex(SourceProtocolError, "no longer available"):
                await UdataProvider().acquire(
                    source, external_id="roads", title="Roads", resource_id="csv"
                )


class DcatTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _graph() -> object:
        from rdflib import Graph

        graph = Graph()
        graph.parse(data=TURTLE_CATALOG, format="turtle")
        return graph

    def test_distribution_metadata_is_read_from_the_graph(self) -> None:
        from rdflib import URIRef

        [descriptor] = graph_resources(self._graph(), URIRef("https://data.example/dataset/roads"))
        self.assertEqual(descriptor["url"], "https://data.example/files/roads.csv")
        self.assertEqual(descriptor["media_type"], "text/csv")
        self.assertEqual(descriptor["format"], "CSV")
        self.assertEqual(descriptor["size_bytes"], 20480)
        self.assertEqual(descriptor["filename"], "roads.csv")

    def test_an_access_url_is_used_when_no_download_url_is_published(self) -> None:
        from rdflib import Graph, URIRef

        graph = Graph()
        graph.parse(
            data=TURTLE_CATALOG.replace("dcat:downloadURL", "dcat:accessURL"), format="turtle"
        )
        [descriptor] = graph_resources(graph, URIRef("https://data.example/dataset/roads"))
        self.assertEqual(descriptor["url"], "https://data.example/files/roads.csv")

    def test_a_non_http_distribution_is_dropped(self) -> None:
        from rdflib import Graph, URIRef

        graph = Graph()
        graph.parse(
            data=TURTLE_CATALOG.replace(
                "<https://data.example/files/roads.csv>", "<ftp://data.example/files/roads.csv>"
            ),
            format="turtle",
        )
        self.assertEqual(graph_resources(graph, URIRef("https://data.example/dataset/roads")), [])

    async def test_search_ranks_datasets_parsed_from_turtle(self) -> None:
        source = dcat_source()
        with patch.object(DcatProvider, "_graph", new=AsyncMock(return_value=self._graph())):
            results = await DcatProvider().search(
                source, prepare_search_intent("road network", source), 5
            )
        self.assertEqual([item.title for item in results], ["Road network"])
        self.assertEqual(results[0].payload["resources"][0]["media_type"], "text/csv")

    async def test_a_dataset_that_left_the_catalog_cannot_be_acquired(self) -> None:
        source = dcat_source()
        with patch.object(DcatProvider, "_graph", new=AsyncMock(return_value=self._graph())):
            with self.assertRaisesRegex(SourceProtocolError, "no longer available"):
                await DcatProvider().acquire(
                    source,
                    external_id="https://data.example/dataset/gone",
                    title="Gone",
                    resource_id="whatever",
                )


if __name__ == "__main__":
    unittest.main()


class PublicDetectionTestCase(unittest.IsolatedAsyncioTestCase):
    """Pins the public source-detection chain before it becomes registry-driven.

    Detection is reached only through the opt-in live smoke, so these fake one
    portal at a time and assert which provider claims the URL.
    """

    @staticmethod
    def _routed(routes: dict[str, tuple[int, str, bytes]]):
        """Serve only the exact paths a real portal would answer; 404 otherwise.

        Prefix matching would let a portal root shadow its own API paths and
        make every probe look like it succeeded.
        """

        def match(url: str) -> tuple[int, str, bytes] | None:
            path = url.split("?", 1)[0]
            return routes.get(path) or routes.get(path.rstrip("/"))

        async def request(
            _self: object,
            _method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json_body: object = None,
            max_bytes: int | None = None,
            credentialed: bool = False,
        ) -> HttpPayload:
            found = match(url)
            if found is None:
                return HttpPayload(
                    url=url, status=404, content_type="text/plain", body=b"", headers={}
                )
            status, content_type, body = found
            return HttpPayload(
                url=url, status=status, content_type=content_type, body=body, headers={}
            )

        return request

    async def test_a_direct_openapi_document_is_claimed_by_the_openapi_provider(self) -> None:
        spec = json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Sensor API", "description": "Readings"},
                "servers": [{"url": "https://api.example/v1"}],
                "paths": {"/readings": {"get": {"operationId": "listReadings", "responses": {}}}},
            }
        ).encode()
        routes = {"https://api.example/openapi.json": (200, "application/json", spec)}
        with (
            patch.object(BoundedHttpClient, "request", self._routed(routes)),
            patch(
                "wotbot.discovery.providers.openapi.resolve_public",
                new=AsyncMock(return_value=(None, [])),
            ),
        ):
            source, evidence, supported = await resolve_public_source(
                "https://api.example/openapi.json"
            )
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "openapi")
        self.assertEqual(source.title, "Sensor API")
        self.assertTrue(any("OpenAPI" in line for line in evidence))

    async def test_a_404_api_base_is_detected_through_its_openapi_json(self) -> None:
        base = "https://metapi.ana.lu/api/v1"
        spec_url = f"{base}/openapi.json"
        spec = json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Weather API backend", "version": "1"},
                "servers": [{"url": "/api/v1"}],
                "paths": {"/hvd": {"get": {"operationId": "readHvd", "responses": {}}}},
            }
        ).encode()
        routes = {spec_url: (200, "application/json", spec)}
        with (
            patch.object(BoundedHttpClient, "request", self._routed(routes)),
            patch(
                "wotbot.discovery.providers.openapi.resolve_public", new=AsyncMock()
            ) as validate_server,
        ):
            source, evidence, supported = await resolve_public_source(base)
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "openapi")
        self.assertEqual(source.title, "Weather API backend")
        self.assertEqual(source.external_id, spec_url)
        self.assertEqual(source.config["url"], spec_url)
        validate_server.assert_awaited_once_with(base)
        self.assertTrue(any("HTTP 404" in line for line in evidence))
        self.assertTrue(any("Trying conventional OpenAPI" in line for line in evidence))

    async def test_a_documentation_page_is_followed_to_the_spec_it_declares(self) -> None:
        """The URL a catalog hands out is usually the docs page, not the document.

        Swagger UI names its specification in the page it serves, so following
        that one declaration turns a human-facing URL into a usable source. The
        source's identity must still be the specification, because that is what
        refresh re-fetches.
        """

        spec = json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Weather API"},
                "servers": [{"url": "https://api.example/v1"}],
                "paths": {"/obs": {"get": {"operationId": "listObs", "responses": {}}}},
            }
        ).encode()
        docs = b"""<html><body><div id="swagger-ui"></div><script>
            SwaggerUIBundle({url: '/v1/openapi.json', dom_id: '#swagger-ui'})
        </script></body></html>"""
        routes = {
            "https://api.example/v1/docs": (200, "text/html", docs),
            "https://api.example/v1/openapi.json": (200, "application/json", spec),
        }
        with (
            patch.object(BoundedHttpClient, "request", self._routed(routes)),
            patch(
                "wotbot.discovery.providers.openapi.resolve_public",
                new=AsyncMock(return_value=(None, [])),
            ),
        ):
            source, evidence, supported = await resolve_public_source("https://api.example/v1/docs")
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "openapi")
        self.assertEqual(source.title, "Weather API")
        self.assertEqual(source.external_id, "https://api.example/v1/openapi.json")
        self.assertEqual(source.config["url"], "https://api.example/v1/openapi.json")
        self.assertTrue(any("declares its API description" in line for line in evidence))

    async def test_a_specification_declared_on_another_host_is_not_followed(self) -> None:
        """One extra request must not become a fetch of a stranger's host."""

        docs = b"""<html><script>
            SwaggerUIBundle({url: 'https://elsewhere.example/openapi.json'})
        </script></html>"""
        routes = {"https://api.example/v1/docs": (200, "text/html", docs)}
        with (
            patch.object(BoundedHttpClient, "request", self._routed(routes)),
            patch(
                "wotbot.discovery.providers.openapi.resolve_public",
                new=AsyncMock(return_value=(None, [])),
            ),
        ):
            source, _evidence, supported = await resolve_public_source(
                "https://api.example/v1/docs"
            )
        self.assertFalse(supported)
        self.assertIsNone(source)

    async def test_a_toolhive_registry_is_claimed_before_any_html_probe(self) -> None:
        workloads = json.dumps(
            [{"name": "files", "status": "running", "url": "http://127.0.0.1:41100/mcp"}]
        ).encode()
        routes = {"https://hive.example/api/v1beta/workloads": (200, "application/json", workloads)}
        with patch.object(BoundedHttpClient, "request", self._routed(routes)):
            source, _evidence, supported = await resolve_public_source("https://hive.example")
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "toolhive")

    async def test_a_udata_portal_is_detected_through_its_api(self) -> None:
        homepage = b"<html lang='en'><head><title>Open Data</title></head><body></body></html>"
        datasets = json.dumps({"data": [{"id": "roads", "title": "Roads"}]}).encode()
        routes = {
            "https://data.example/api/1/datasets": (200, "application/json", datasets),
            "https://data.example": (200, "text/html", homepage),
        }
        with patch.object(BoundedHttpClient, "request", self._routed(routes)):
            source, _evidence, supported = await resolve_public_source("https://data.example")
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "udata")
        self.assertEqual(source.title, "Open Data")

    async def test_a_dcat_catalog_is_detected_when_no_udata_api_answers(self) -> None:
        homepage = (
            b"<html><head><title>Catalog</title></head>"
            b"<body><a href='/catalog.rdf'>catalog</a></body></html>"
        )
        routes = {
            "https://rdf.example/catalog.rdf": (200, "text/turtle", TURTLE_CATALOG.encode()),
            "https://rdf.example": (200, "text/html", homepage),
        }
        with patch.object(BoundedHttpClient, "request", self._routed(routes)):
            source, _evidence, supported = await resolve_public_source("https://rdf.example")
        self.assertTrue(supported)
        assert source is not None
        self.assertEqual(source.provider, "dcat")

    async def test_an_unrecognized_site_is_reported_unsupported_with_evidence(self) -> None:
        homepage = b"<html><head><title>Just a site</title></head><body></body></html>"
        routes = {"https://plain.example": (200, "text/html", homepage)}
        with patch.object(BoundedHttpClient, "request", self._routed(routes)):
            source, evidence, supported = await resolve_public_source("https://plain.example")
        self.assertFalse(supported)
        self.assertIsNone(source)
        self.assertTrue(evidence)


class PublicRedirectPolicyTestCase(unittest.IsolatedAsyncioTestCase):
    """Measured against real portals: refusing every origin change was too strict."""

    @staticmethod
    def _redirecting(location: str) -> MagicMock:
        redirect = MagicMock(status=302, headers={"Location": location})
        redirect.release = MagicMock()
        final = MagicMock(status=200, headers={})
        session = MagicMock()
        session.request = AsyncMock(side_effect=[redirect, final])
        session.close = AsyncMock()
        return session

    async def test_a_public_probe_may_upgrade_http_to_https(self) -> None:
        session = self._redirecting("https://portal.example/")
        client = BoundedHttpClient(mode="public", max_requests=None)
        with (
            patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session),
            patch(
                "wotbot.discovery.http.resolve_public",
                new=AsyncMock(return_value=(urlparse("https://portal.example/"), ["203.0.113.1"])),
            ),
        ):
            _session, response = await client.stream("http://portal.example/")
        self.assertEqual(response.status, 200)

    async def test_a_public_probe_may_follow_a_bare_to_www_redirect(self) -> None:
        session = self._redirecting("https://www.portal.example/data/")
        client = BoundedHttpClient(mode="public", max_requests=None)
        with (
            patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session),
            patch(
                "wotbot.discovery.http.resolve_public",
                new=AsyncMock(
                    return_value=(urlparse("https://www.portal.example/data/"), ["203.0.113.1"])
                ),
            ),
        ):
            _session, response = await client.stream("https://portal.example/data/")
        self.assertEqual(response.status, 200)

    async def test_the_address_policy_still_runs_on_every_hop(self) -> None:
        """Allowing an origin change must not let a redirect reach a private address."""
        session = self._redirecting("https://internal.example/")
        client = BoundedHttpClient(mode="public", max_requests=None)
        with (
            patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session),
            patch(
                "wotbot.discovery.http.resolve_public",
                new=AsyncMock(
                    side_effect=[
                        (urlparse("https://portal.example/"), ["203.0.113.1"]),
                        UnsafeUrlError("Public source resolves to a non-public network address"),
                    ]
                ),
            ),
            self.assertRaisesRegex(UnsafeUrlError, "non-public network address"),
        ):
            await client.stream("https://portal.example/")

    async def test_a_credentialled_public_request_still_cannot_change_origin(self) -> None:
        session = self._redirecting("https://elsewhere.example/")
        client = BoundedHttpClient(mode="public", max_requests=None)
        with (
            patch("wotbot.discovery.http.aiohttp.ClientSession", return_value=session),
            patch(
                "wotbot.discovery.http.resolve_public",
                new=AsyncMock(return_value=(urlparse("https://portal.example/"), ["203.0.113.1"])),
            ),
            self.assertRaisesRegex(UnsafeUrlError, "cannot redirect across origins"),
        ):
            await client.stream(
                "https://portal.example/",
                headers={"Authorization": "secret"},
                credentialed=True,
            )


class DetectionRegistryTestCase(unittest.TestCase):
    """Detection order comes from the registry, not from a hardcoded chain."""

    def test_only_providers_declaring_detect_are_probed(self) -> None:
        names = [provider.name for provider in detectors(PROVIDERS)]
        self.assertEqual(
            set(names),
            {name for name, provider in PROVIDERS.items() if "detect" in provider.capabilities},
        )
        self.assertNotIn("edc-v3", names)

    def test_specific_probes_run_before_ones_that_guess_from_a_homepage(self) -> None:
        names = [provider.name for provider in detectors(PROVIDERS)]
        self.assertLess(names.index("openapi"), names.index("udata"))
        self.assertLess(names.index("toolhive"), names.index("udata"))
        self.assertLess(names.index("udata"), names.index("dcat"))

    def test_a_new_detecting_provider_joins_the_chain_by_declaration_alone(self) -> None:
        class Invented(DcatProvider):
            name = "invented"
            detect_priority = 1

        extended = {**PROVIDERS, "invented": Invented()}
        self.assertEqual(detectors(extended)[0].name, "invented")

    def test_public_detection_does_not_name_any_provider(self) -> None:
        source = pathlib.Path(public_module.__file__).read_text() if public_module.__file__ else ""
        for name in PROVIDERS:
            self.assertNotIn(f'"{name}"', source)


class ConnectionBudgetTestCase(unittest.IsolatedAsyncioTestCase):
    """Phase 3: a page of results must not cost a connection per row."""

    async def asyncSetUp(self) -> None:
        await reset_clients()

    async def asyncTearDown(self) -> None:
        await reset_clients()

    async def test_a_page_of_candidates_is_written_in_one_round_trip(self) -> None:
        writes: list[tuple[str, int]] = []

        class Pipeline:
            def set(self, key: str, _value: str, *, ex: int) -> Pipeline:
                writes.append((key, ex))
                return self

            async def execute(self) -> list[bool]:
                return [True] * len(writes)

        client = MagicMock()
        client.aclose = AsyncMock()
        client.pipeline = MagicMock(return_value=Pipeline())
        records = [
            CandidateRecord.from_draft(
                CandidateDraft(
                    provider="udata",
                    source_id="source-a",
                    external_id=f"dataset-{index}",
                    kind="dataset",
                    title=f"Dataset {index}",
                ),
                scope_kind="thread",
                scope_id="thread-a",
            )
            for index in range(25)
        ]
        with patch("wotbot.discovery.store.redis.from_url", return_value=client) as from_url:
            identifiers = await CandidateStore("redis://x").put_many(records)

        self.assertEqual(len(identifiers), 25)
        self.assertEqual(len(set(identifiers)), 25, "candidate ids must be unguessable and unique")
        self.assertEqual(len(writes), 25)
        # One client, one pipeline: not one connection per candidate.
        from_url.assert_called_once()
        client.pipeline.assert_called_once()

    async def test_the_pooled_client_is_reused_across_operations(self) -> None:
        client = MagicMock()
        client.aclose = AsyncMock()
        client.get = AsyncMock(return_value=None)
        with patch("wotbot.discovery.store.redis.from_url", return_value=client) as from_url:
            store = DownloadStore("redis://x")
            for _ in range(5):
                with self.assertRaises(ValueError):
                    await store.get("a" * 43)
        # Five lookups, one client: the pool is reused instead of reconnecting.
        from_url.assert_called_once()
        self.assertEqual(client.get.await_count, 5)


class SourceListingQueryTestCase(unittest.TestCase):
    """Phase 3: rendering a page must not open two sessions per row."""

    def test_the_management_page_uses_one_session_and_two_batch_queries(self) -> None:
        records = [
            SourceRecord(
                id=f"source-{index}",
                provider="udata",
                external_id=f"https://data{index}.example",
                title=f"Portal {index}",
                description="",
                tags=[],
                config={"url": f"https://data{index}.example"},
                network_access="public",
                security_name="source_sc",
                security_scheme="nosec",
            )
            for index in range(25)
        ]
        session = MagicMock()
        with (
            patch("wotbot.discovery.service.credential_schemes", return_value={}) as schemes,
            patch("wotbot.discovery.service.dependent_counts", return_value={}) as counts,
        ):
            items = _management_sources(session, records)

        self.assertEqual(len(items), 25)
        schemes.assert_called_once()
        counts.assert_called_once()
        self.assertEqual(items[0]["dependent_thing_count"], 0)
        self.assertNotIn("credentials", json.dumps(items))
