from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from wotbot.catalog import validate_document
from wotbot.catalog.service import _validate_protected_resource_update
from wotbot.discovery.errors import StaleCandidateError
from wotbot.discovery.http import HttpPayload
from wotbot.discovery.models import CandidateDraft, DownloadRecord, SourceDefinition
from wotbot.discovery.providers.base import (
    provider_action_href,
    provider_download_action,
    provider_thing_id,
)
from wotbot.discovery.providers.edc_v3 import (
    EDC_PROPERTIES_URI,
    TXB_API_DESCRIPTION,
    TXB_API_DESCRIPTION_URI,
    EdcV3Provider,
    edc_api_description,
)
from wotbot.discovery.search import prepare_search_intent


def edc_source() -> SourceDefinition:
    return SourceDefinition(
        id="urn:wotbot:source:edc-v3:test",
        provider="edc-v3",
        title="Partner dataspace",
        external_id="https://provider.example/protocol",
        config={
            "management_url": "https://edc.example/management",
            "counter_party_address": "https://provider.example/protocol",
            "protocol": "dataspace-protocol-http",
            "poll_interval_seconds": 0,
            "poll_timeout_seconds": 5,
        },
        security_scheme="apikey",
        credential={"apiKey": "secret"},
    )


def api_document(*, operation_count: int = 1) -> dict:
    paths: dict[str, dict] = {}
    for index in reversed(range(operation_count)):
        paths[f"/orders/{index}/{{orderId}}"] = {
            "get": {
                "operationId": f"getOrder{index}",
                "summary": f"Get order {index}",
                "description": f"Returns order {index}",
                "parameters": [
                    {
                        "name": "expand",
                        "in": "query",
                        "schema": {"type": "boolean"},
                    },
                    {
                        "name": "orderId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Order"}}
                        },
                    }
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Orders API",
            "version": "2026-08",
            "description": "Partner order lookup",
        },
        "servers": [{"url": "https://secret.internal.example/api"}],
        "security": [{"oauth": ["orders:read"]}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "oauth": {
                    "type": "oauth2",
                    "flows": {
                        "clientCredentials": {
                            "tokenUrl": "https://secret.internal.example/token",
                            "scopes": {"orders:read": "Read orders"},
                        }
                    },
                }
            },
            "schemas": {
                "Order": {
                    "type": "object",
                    "properties": {"privateField": {"type": "string"}},
                }
            },
        },
    }


def dataset(metadata: object = None, *, include_metadata: bool = True) -> dict:
    value = {
        "@id": "asset-1",
        "dct:title": {"@value": "Partner orders"},
        "dct:description": "Shared order metadata",
        "odrl:hasPolicy": {"@id": "policy-1", "@type": "Offer"},
    }
    if include_metadata:
        value[TXB_API_DESCRIPTION] = json.dumps(api_document()) if metadata is None else metadata
    return value


def catalog(value: dict) -> dict:
    return {"dcat:dataset": [value]}


def candidate_for(value: dict) -> CandidateDraft:
    metadata = edc_api_description(value)
    return CandidateDraft(
        provider="edc-v3",
        source_id=edc_source().id,
        external_id="asset-1",
        kind="dataspace-api" if metadata.summary else "dataspace-asset",
        title="Search result title",
        summary="Search result description",
        payload={"spec_digest": metadata.fingerprint, "compiler_version": 3},
    )


class EdcApiDescriptionExtractionTestCase(unittest.TestCase):
    def test_tx_bootstrap_encodings_have_the_same_canonical_fingerprint(self) -> None:
        document = api_document()
        encoded = json.dumps(document)
        values = (
            {TXB_API_DESCRIPTION: encoded},
            {TXB_API_DESCRIPTION_URI: document},
            {"properties": {TXB_API_DESCRIPTION: {"@value": encoded}}},
            {"edc:properties": {TXB_API_DESCRIPTION_URI: [{"@value": encoded}]}},
            {EDC_PROPERTIES_URI: {TXB_API_DESCRIPTION: [document]}},
        )

        extracted = [edc_api_description(value) for value in values]

        self.assertTrue(all(item.summary is not None for item in extracted))
        self.assertEqual(len({item.fingerprint for item in extracted}), 1)
        self.assertEqual(len({json.dumps(item.summary, sort_keys=True) for item in extracted}), 1)

    def test_absent_metadata_is_distinct_from_invalid_metadata(self) -> None:
        absent = edc_api_description(dataset(include_metadata=False))
        invalid = edc_api_description(dataset("not-json"))

        self.assertEqual(absent.fingerprint, "absent")
        self.assertEqual(absent.warning, "")
        self.assertIsNone(absent.summary)
        self.assertTrue(invalid.fingerprint.startswith("invalid:"))
        self.assertIn("invalid OpenAPI metadata", invalid.warning)
        self.assertIsNone(invalid.summary)

    def test_unsafe_or_unsupported_metadata_falls_back(self) -> None:
        swagger = api_document()
        swagger.pop("openapi")
        swagger["swagger"] = "2.0"

        unsafe_path = api_document()
        unsafe_path["paths"] = {"/%252e%252e/private": {"get": {}}}

        external_reference = api_document()
        external_reference["paths"]["/orders/0/{orderId}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] = {"$ref": "https://example.test/schema.json"}

        too_deep = api_document()
        nested: dict = {}
        too_deep["x-deep"] = nested
        for _ in range(110):
            child: dict = {}
            nested["child"] = child
            nested = child

        values = (
            "{" + "x" * (4 * 1024 * 1024) + "}",
            swagger,
            unsafe_path,
            external_reference,
            too_deep,
        )
        for value in values:
            with self.subTest(kind=type(value).__name__):
                result = edc_api_description(dataset(value))
                self.assertIsNone(result.summary)
                self.assertTrue(result.fingerprint.startswith("invalid:"))
                self.assertTrue(result.warning)

    def test_summary_is_bounded_sorted_and_excludes_raw_openapi_sections(self) -> None:
        result = edc_api_description(dataset(api_document(operation_count=31)))

        self.assertIsNotNone(result.summary)
        summary = result.summary or {}
        self.assertEqual(summary["operationCount"], 31)
        self.assertEqual(len(summary["operations"]), 30)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["title"], "Orders API")
        self.assertEqual(summary["version"], "2026-08")
        self.assertEqual(summary["specificationVersion"], "3.1.0")
        paths = [operation["path"] for operation in summary["operations"]]
        self.assertEqual(paths, sorted(paths))
        serialized = json.dumps(summary)
        for excluded in (
            "servers",
            "securitySchemes",
            "schemas",
            "secret.internal.example",
            "privateField",
        ):
            self.assertNotIn(excluded, serialized)

    def test_operations_are_sorted_by_path_and_method(self) -> None:
        document = api_document(operation_count=0)
        document["paths"] = {
            "/z": {"post": {"operationId": "postZ"}, "get": {"operationId": "getZ"}},
            "/a": {"put": {"operationId": "putA"}},
        }

        result = edc_api_description(dataset(document))

        operations = (result.summary or {})["operations"]
        self.assertEqual(
            [(item["path"], item["method"]) for item in operations],
            [("/a", "PUT"), ("/z", "GET"), ("/z", "POST")],
        )

    def test_internal_path_item_references_are_summarized(self) -> None:
        document = api_document(operation_count=0)
        document["components"]["pathItems"] = {
            "Orders": {"get": {"operationId": "listOrders", "summary": "List orders"}}
        }
        document["paths"] = {"/orders": {"$ref": "#/components/pathItems/Orders"}}

        result = edc_api_description(dataset(document))

        self.assertEqual((result.summary or {})["operationCount"], 1)
        self.assertEqual(
            (result.summary or {})["operations"][0]["operationId"],
            "listOrders",
        )


class EdcApiDescriptionOnboardingTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_valid_metadata_creates_one_thing_with_compiled_actions(self) -> None:
        provider = EdcV3Provider()
        source = edc_source()
        first = dataset(api_document(operation_count=31))
        latest = deepcopy(first)
        latest["dct:title"] = "Current partner title"
        latest["dct:description"] = "Current partner description"
        sender = AsyncMock(side_effect=[catalog(first), latest])

        with patch("wotbot.discovery.providers.edc_v3.source_json", new=sender):
            candidates = await provider.search(
                source,
                prepare_search_intent("orders", source),
                10,
            )
            result = await provider.onboarding_document(
                source,
                candidates[0],
                runtime=AsyncMock(),
            )

        validate_document(result.document)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "dataspace-api")
        self.assertGreater(candidates[0].payload["compiler_version"], 2)
        self.assertEqual(
            result.document["wotbot:generation"]["compilerVersion"],
            candidates[0].payload["compiler_version"],
        )
        self.assertEqual(
            set(candidates[0].payload),
            {"spec_digest", "compiler_version"},
        )
        self.assertNotIn("paths", json.dumps(candidates[0].payload))
        self.assertEqual(result.document["title"], "Current partner title")
        self.assertEqual(result.document["description"], "Current partner description")
        self.assertEqual(len(result.document["actions"]), 30)
        action = result.document["actions"]["getOrder0"]
        self.assertEqual(action["output"]["properties"]["privateField"]["type"], "string")
        self.assertEqual(action["uriVariables"]["orderId"]["type"], "string")
        self.assertEqual(action["wotbot:generatedBy"], "edc-v3")
        self.assertEqual(
            action["forms"][0]["href"],
            provider_action_href(result.document["id"], "getOrder0") + "{?orderId,expand}",
        )
        self.assertEqual(action["forms"][0]["wotbot:providerOperation"], "invoke")
        self.assertEqual(action["forms"][0]["wotbot:path"], "/orders/0/{orderId}")
        self.assertEqual(
            result.document["wotbot:apiDescription"]["wotbot:generatedBy"],
            "edc-v3",
        )
        self.assertTrue(any("first 30 of 31" in warning for warning in result.warnings))
        serialized = json.dumps(result.document)
        self.assertNotIn("secret.internal.example", serialized)
        dataset_request = sender.await_args_list[1]
        self.assertTrue(dataset_request.args[1].endswith("/v3/catalog/dataset/request"))
        self.assertEqual(dataset_request.kwargs["body"]["@type"], "DatasetRequest")
        self.assertEqual(dataset_request.kwargs["body"]["@id"], "asset-1")

    async def test_search_does_not_copy_a_raw_spec_from_the_asset_description(self) -> None:
        offered = dataset(api_document())
        offered["dct:description"] = json.dumps(api_document())
        offered["dct:abstract"] = "Human-readable asset summary"
        source = edc_source()

        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog(offered)),
        ):
            candidates = await EdcV3Provider().search(
                source,
                prepare_search_intent("orders", source),
                10,
            )

        self.assertEqual(candidates[0].summary, "Human-readable asset summary")
        self.assertNotIn("components", candidates[0].summary)

    async def test_absent_and_invalid_metadata_use_only_the_download_fallback(self) -> None:
        provider = EdcV3Provider()
        for value in (dataset(include_metadata=False), dataset("not-json")):
            with self.subTest(fingerprint=edc_api_description(value).fingerprint):
                with patch(
                    "wotbot.discovery.providers.edc_v3.source_json",
                    new=AsyncMock(return_value=catalog(value)),
                ):
                    result = await provider.onboarding_document(
                        edc_source(),
                        candidate_for(value),
                        runtime=AsyncMock(),
                    )

                validate_document(result.document)
                self.assertEqual(set(result.document["actions"]), {"download_asset"})
                action = result.document["actions"]["download_asset"]
                self.assertEqual(action["wotbot:generatedBy"], "edc-v3")
                self.assertEqual(
                    action["forms"][0]["wotbot:providerOperation"],
                    "download",
                )
                self.assertNotIn("wotbot:apiDescription", result.document)
                self.assertEqual(bool(result.warnings), value.get(TXB_API_DESCRIPTION) is not None)

    async def test_onboarding_rejects_a_changed_or_missing_fingerprint(self) -> None:
        current = dataset(api_document())
        changed_spec = api_document()
        changed_spec["info"]["version"] = "new"
        changed = dataset(changed_spec)
        provider = EdcV3Provider()

        for payload in ({"spec_digest": ""}, candidate_for(current).payload):
            candidate = CandidateDraft(
                provider="edc-v3",
                source_id=edc_source().id,
                external_id="asset-1",
                kind="dataspace-api",
                title="Orders",
                payload=payload,
            )
            with (
                self.subTest(payload=payload),
                patch(
                    "wotbot.discovery.providers.edc_v3.source_json",
                    new=AsyncMock(return_value=catalog(changed)),
                ),
                self.assertRaisesRegex(StaleCandidateError, "changed"),
            ):
                await provider.onboarding_document(
                    edc_source(),
                    candidate,
                    runtime=AsyncMock(),
                )


class EdcApiDescriptionRefreshTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_supports_download_to_api_and_preserves_manual_content(self) -> None:
        provider = EdcV3Provider()
        absent = dataset(include_metadata=False)
        current = provider._onboarding_result(
            candidate_for(absent),
            edc_api_description(absent),
        ).document
        current["title"] = "Local title"
        current["description"] = "Local description"
        current["properties"] = {
            "manual": {
                "type": "string",
                "forms": [{"href": "https://manual.example.test/value"}],
            }
        }
        current["actions"]["manualAction"] = {
            "forms": [{"href": "https://manual.example.test/run"}]
        }

        offered = dataset(api_document(operation_count=2))
        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog(offered)),
        ):
            generated = await provider.refresh_document(
                edc_source(),
                current,
                external_id="asset-1",
                runtime=AsyncMock(),
            )
        merged, removed_credentials = provider.merge_refresh(current, generated.document)
        diff = provider.refresh_diff(current, merged)

        validate_document(merged)
        self.assertEqual(merged["title"], "Local title")
        self.assertEqual(merged["description"], "Local description")
        self.assertIn("manual", merged["properties"])
        self.assertEqual(
            set(merged["actions"]),
            {"manualAction", "getOrder0", "getOrder1"},
        )
        self.assertIn("wotbot:apiDescription", merged)
        self.assertEqual(diff["removed_actions"], ["download_asset"])
        self.assertEqual(diff["added_actions"], ["getOrder0", "getOrder1"])
        self.assertTrue(diff["metadata_changed"])
        self.assertEqual(removed_credentials, ())

    async def test_refresh_supports_api_to_download(self) -> None:
        provider = EdcV3Provider()
        offered = dataset(api_document())
        current = provider._onboarding_result(
            candidate_for(offered),
            edc_api_description(offered),
        ).document
        invalid = dataset("not-json")

        with patch(
            "wotbot.discovery.providers.edc_v3.source_json",
            new=AsyncMock(return_value=catalog(invalid)),
        ):
            generated = await provider.refresh_document(
                edc_source(),
                current,
                external_id="asset-1",
                runtime=AsyncMock(),
            )
        merged, _ = provider.merge_refresh(current, generated.document)
        diff = provider.refresh_diff(current, merged)

        self.assertNotIn("wotbot:apiDescription", merged)
        self.assertEqual(set(merged["actions"]), {"download_asset"})
        self.assertEqual(diff["added_actions"], ["download_asset"])
        self.assertEqual(diff["removed_actions"], ["getOrder0"])
        self.assertTrue(diff["metadata_changed"])
        self.assertEqual(len(generated.warnings), 1)

    def test_changed_invalid_metadata_is_visible_in_the_refresh_diff(self) -> None:
        provider = EdcV3Provider()
        first = dataset("invalid-one")
        second = dataset("invalid-two")
        current = provider._onboarding_result(
            candidate_for(first),
            edc_api_description(first),
        ).document
        generated = provider._onboarding_result(
            candidate_for(second),
            edc_api_description(second),
        ).document

        merged, _ = provider.merge_refresh(current, generated)
        diff = provider.refresh_diff(current, merged)

        self.assertTrue(diff["metadata_changed"])
        self.assertEqual(diff["added_actions"], [])
        self.assertEqual(diff["removed_actions"], [])

    def test_first_refresh_recognizes_the_legacy_unmarked_download_action(self) -> None:
        provider = EdcV3Provider()
        thing_id = provider_thing_id("edc-v3", edc_source().id, "asset-1")
        legacy = {
            "@context": [
                "https://www.w3.org/2022/wot/td/v1.1",
                {"wotbot": "https://wotbot.dev/ontology#"},
            ],
            "id": thing_id,
            "title": "Legacy",
            "security": ["nosec_sc"],
            "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
            "actions": {
                "download_asset": provider_download_action(
                    thing_id,
                    action_name="download_asset",
                    title="Download asset",
                    description=(
                        "Negotiate access through the dataspace and return the asset content."
                    ),
                ),
                "manualAction": {"forms": [{"href": "https://manual.example.test/run"}]},
            },
        }
        offered = dataset(api_document())
        generated = provider._onboarding_result(
            candidate_for(offered),
            edc_api_description(offered),
        ).document

        merged, _ = provider.merge_refresh(legacy, generated)
        diff = provider.refresh_diff(legacy, merged)

        validate_document(merged)
        self.assertEqual(set(merged["actions"]), {"manualAction", "getOrder0"})
        self.assertIn("wotbot:apiDescription", merged)
        self.assertEqual(diff["removed_actions"], ["download_asset"])
        self.assertEqual(diff["added_actions"], ["getOrder0"])
        self.assertTrue(diff["metadata_changed"])

    def test_generated_api_metadata_is_protected_from_generic_updates(self) -> None:
        provider = EdcV3Provider()
        offered = dataset(api_document())
        current = provider._onboarding_result(
            candidate_for(offered),
            edc_api_description(offered),
        ).document
        replacement = deepcopy(current)
        replacement["wotbot:apiDescription"]["title"] = "Edited"

        with self.assertRaises(HTTPException) as raised:
            _validate_protected_resource_update(
                current,
                replacement,
                provider="edc-v3",
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_generated_edc_actions_are_protected_from_generic_updates(self) -> None:
        provider = EdcV3Provider()
        offered = dataset(api_document())
        current = provider._onboarding_result(
            candidate_for(offered),
            edc_api_description(offered),
        ).document
        replacement = deepcopy(current)
        replacement["actions"]["getOrder0"]["title"] = "Edited"

        with self.assertRaises(HTTPException) as raised:
            _validate_protected_resource_update(
                current,
                replacement,
                provider="edc-v3",
            )

        self.assertEqual(raised.exception.status_code, 409)


class EdcApiInvocationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_api_invocation_negotiates_then_calls_the_acquired_endpoint(self) -> None:
        provider = EdcV3Provider()
        client = AsyncMock()
        client.request.return_value = HttpPayload(
            url="https://api.example/base/orders/A%2FB?expand=true",
            status=200,
            content_type="application/json",
            body=b'{"ok":true}',
            headers={},
        )
        capability = DownloadRecord(
            endpoint="https://api.example/base",
            headers={"Authorization": "Bearer secret"},
        )

        with (
            patch.object(
                provider,
                "acquire",
                new=AsyncMock(return_value=(capability, 60)),
            ) as acquire,
            patch(
                "wotbot.discovery.providers.edc_v3.source_client",
                return_value=client,
            ),
        ):
            response = await provider.invoke_api(
                edc_source(),
                external_id="asset-1",
                method="POST",
                path="/orders/{orderId}",
                path_variables=("orderId",),
                query_variables=("expand",),
                uri_variables={"orderId": "A/B", "expand": True},
                input_data={"name": "Ada"},
            )

        acquire.assert_awaited_once()
        client.request.assert_awaited_once_with(
            "POST",
            "https://api.example/base/orders/A%2FB?expand=true",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer secret",
            },
            json_body={"name": "Ada"},
            max_bytes=512 * 1024,
            credentialed=True,
        )
        self.assertEqual(response.body, b'{"ok":true}')
        self.assertEqual(response.content_type, "application/json")
