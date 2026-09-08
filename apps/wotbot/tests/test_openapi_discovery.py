from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from wotbot.catalog import validate_document
from wotbot.catalog.service import _validate_protected_resource_update
from wotbot.discovery.detection import DetectionContext
from wotbot.discovery.errors import StaleCandidateError
from wotbot.discovery.http import HttpPayload
from wotbot.discovery.models import (
    CandidateDraft,
    RefreshRecord,
    SearchIntent,
    SourceDefinition,
)
from wotbot.discovery.providers.openapi import (
    OpenApiError,
    OpenApiProvider,
    ParsedApi,
    collect_operations,
    compile_thing,
    declared_spec_url,
    group_external_id,
    openapi_version,
    operation_groups,
    parse_openapi,
    resolve_server,
    select_security,
)
from wotbot.discovery.store import RefreshStore


def api_document(*, operation_count: int = 1) -> dict:
    paths = {}
    for index in range(operation_count):
        paths[f"/pets/{index}/{{petId}}"] = {
            "get": {
                "operationId": "getPet" if index < 2 else f"getPet{index}",
                "summary": f"Get pet {index}",
                "tags": ["pets"],
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "verbose",
                        "in": "query",
                        "schema": {"type": "boolean"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                        },
                    }
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Pet API", "version": "1.0", "description": "Pets"},
        "servers": [
            {
                "url": "https://{region}.api.example.test/v1",
                "variables": {"region": {"default": "eu"}},
            }
        ],
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                }
            }
        },
        "paths": paths,
    }


def parsed_api(document: dict | None = None) -> ParsedApi:
    document = document or api_document()
    body = json.dumps(document, sort_keys=True).encode()
    security, definitions, security_warnings = select_security(document)
    operations, operation_warnings = collect_operations(document, security)
    return ParsedApi(
        document=document,
        digest=hashlib.sha256(body).hexdigest(),
        spec_url="https://docs.example.test/openapi.json",
        version=openapi_version(document),
        title=str(document["info"]["title"]),
        description=str(document["info"].get("description") or ""),
        server_url=resolve_server(
            document,
            "https://docs.example.test/openapi.json",
            "",
        ),
        security=security,
        security_definitions=definitions,
        operations=tuple(operations),
        warnings=(*security_warnings, *operation_warnings),
    )


def source() -> SourceDefinition:
    return SourceDefinition(
        id="urn:wotbot:source:openapi:test",
        external_id="https://docs.example.test/openapi.json",
        provider="openapi",
        title="Pet API",
        config={"url": "https://docs.example.test/openapi.json"},
    )


class OpenApiCompilerTestCase(unittest.TestCase):
    def test_parses_json_yaml_and_swagger(self) -> None:
        document = api_document()
        self.assertEqual(
            openapi_version(parse_openapi(json.dumps(document).encode())), "OpenAPI 3.1.0"
        )
        yaml_body = b"openapi: 3.0.3\ninfo:\n  title: Demo\n  version: '1'\npaths: {}\n"
        self.assertEqual(openapi_version(parse_openapi(yaml_body)), "OpenAPI 3.0.3")
        swagger = {
            "swagger": "2.0",
            "info": {"title": "Legacy", "version": "1"},
            "host": "legacy.example.test",
            "basePath": "/v2",
            "schemes": ["https"],
            "paths": {},
        }
        self.assertEqual(
            openapi_version(parse_openapi(json.dumps(swagger).encode())), "Swagger 2.0"
        )
        self.assertEqual(
            resolve_server(swagger, "https://docs.example.test/swagger.json", ""),
            "https://legacy.example.test/v2",
        )

    def test_rejects_invalid_and_excessively_deep_documents(self) -> None:
        with self.assertRaises(OpenApiError):
            parse_openapi(b"not: [valid")
        nested: dict = {"value": None}
        current = nested
        for _ in range(110):
            child = {"value": None}
            current["value"] = child
            current = child
        nested.update({"openapi": "3.0.3", "paths": {}})
        with self.assertRaisesRegex(OpenApiError, "complex"):
            parse_openapi(json.dumps(nested).encode())

    def test_selects_first_server_whose_variables_have_defaults(self) -> None:
        document = api_document()
        document["servers"] = [
            {"url": "https://{region}.invalid.test", "variables": {}},
            {"url": "https://api.example.test/v2"},
        ]
        self.assertEqual(
            resolve_server(document, "https://docs.example.test/openapi.json", ""),
            "https://api.example.test/v2",
        )

    def test_compiles_actions_with_http_forms_schemas_and_stable_names(self) -> None:
        parsed = parsed_api(api_document(operation_count=2))
        group = operation_groups(parsed.operations)[0]
        external_id = group_external_id(source().external_id, parsed.server_url, group.key)
        document, warnings = compile_thing(source(), parsed, group, external_id)
        validate_document(document)
        self.assertFalse(warnings)
        self.assertEqual(set(document["actions"]), {"getPet", "getPet_c08028cc"})
        action = document["actions"]["getPet"]
        self.assertEqual(action["forms"][0]["htv:methodName"], "GET")
        self.assertIn("{petId}{?verbose}", action["forms"][0]["href"])
        self.assertEqual(action["uriVariables"]["petId"]["type"], "string")
        self.assertEqual(action["output"]["properties"]["name"]["type"], "string")
        self.assertNotIn("paths", json.dumps(document))

    def test_normalizes_openapi_31_nullable_type_arrays(self) -> None:
        document = api_document()
        document["components"]["schemas"]["Pet"]["properties"].update(
            {
                "nickname": {"type": ["string", "null"]},
                "ambiguous": {"type": ["string", "integer", "null"]},
            }
        )

        parsed = parsed_api(document)
        td, _warnings = compile_thing(
            source(),
            parsed,
            operation_groups(parsed.operations)[0],
            "nullable",
        )

        properties = td["actions"]["getPet"]["output"]["properties"]
        self.assertEqual(properties["nickname"]["type"], "string")
        self.assertNotIn("type", properties["ambiguous"])

    def test_keeps_optional_parameters_written_as_a_nullable_union(self) -> None:
        """OpenAPI 3.1 spells an optional parameter as ``anyOf: [T, null]``.

        Every FastAPI and Pydantic specification emits that shape, and reading
        it as non-primitive dropped the parameter, leaving an action that could
        not be given the argument its operation is about.
        """

        document = api_document()
        document["paths"]["/pets/0/{petId}"]["get"]["parameters"].extend(
            [
                {
                    "name": "city",
                    "in": "query",
                    "schema": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "title": "City",
                    },
                },
                {
                    "name": "lat",
                    "in": "query",
                    "schema": {
                        "anyOf": [
                            {"type": "number", "minimum": -90, "maximum": 90},
                            {"type": "null"},
                        ]
                    },
                },
            ]
        )

        parsed = parsed_api(document)
        self.assertEqual([line for line in parsed.warnings if "parameter" in line], [])
        td, _warnings = compile_thing(
            source(),
            parsed,
            operation_groups(parsed.operations)[0],
            "nullable-union",
        )

        action = td["actions"]["getPet"]
        variables = action["uriVariables"]
        self.assertEqual(variables["city"]["type"], "integer")
        self.assertEqual(variables["city"]["title"], "City")
        self.assertEqual(variables["lat"]["type"], "number")
        self.assertEqual((variables["lat"]["minimum"], variables["lat"]["maximum"]), (-90, 90))
        self.assertIn("city", action["forms"][0]["href"])
        self.assertIn("lat", action["forms"][0]["href"])

    def test_a_union_of_real_variants_is_kept_as_a_union(self) -> None:
        document = api_document()
        document["components"]["schemas"]["Pet"]["properties"]["age"] = {
            "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]
        }

        parsed = parsed_api(document)
        td, _warnings = compile_thing(
            source(), parsed, operation_groups(parsed.operations)[0], "union"
        )

        age = td["actions"]["getPet"]["output"]["properties"]["age"]
        self.assertEqual([variant["type"] for variant in age["anyOf"]], ["integer", "string"])
        self.assertNotIn("type", age)

    def test_a_schema_without_a_const_does_not_claim_one(self) -> None:
        """``const: null`` asserts the value must be null, so never invent it."""

        document = api_document()
        document["components"]["schemas"]["Pet"]["properties"].update(
            {
                "colour": {"type": "string", "enum": ["red", "blue"]},
                "exact": {"type": "string", "const": "fixed"},
            }
        )

        parsed = parsed_api(document)
        td, _warnings = compile_thing(
            source(), parsed, operation_groups(parsed.operations)[0], "const"
        )

        properties = td["actions"]["getPet"]["output"]["properties"]
        self.assertNotIn("const", properties["colour"])
        self.assertNotIn("default", properties["colour"])
        self.assertEqual(properties["exact"]["const"], "fixed")

    def test_groups_more_than_thirty_operations_by_tag(self) -> None:
        parsed = parsed_api(api_document(operation_count=31))
        groups = operation_groups(parsed.operations)
        self.assertEqual([len(group.operations) for group in groups], [30, 1])
        self.assertEqual([group.key for group in groups], ["tag:pets:part:1", "tag:pets:part:2"])

    def test_compiles_json_post_body_as_action_input(self) -> None:
        document = api_document()
        document["paths"] = {
            "/pets": {
                "post": {
                    "operationId": "createPet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                        },
                    },
                    "responses": {"204": {"description": "created"}},
                }
            }
        }
        parsed = parsed_api(document)
        group = operation_groups(parsed.operations)[0]
        td, _warnings = compile_thing(
            source(),
            parsed,
            group,
            group_external_id(source().external_id, parsed.server_url, group.key),
        )
        action = td["actions"]["createPet"]
        self.assertEqual(action["forms"][0]["htv:methodName"], "POST")
        self.assertEqual(action["input"]["required"], ["name"])
        self.assertFalse(action["safe"])
        self.assertFalse(action["idempotent"])

    def test_maps_supported_security_and_skips_unsupported_operations(self) -> None:
        document = api_document()
        document["components"]["securitySchemes"] = {
            "token": {"type": "apiKey", "in": "header", "name": "X-Token"},
            "oauth": {"type": "oauth2", "flows": {}},
        }
        document["security"] = [{"token": []}]
        first = next(iter(document["paths"].values()))["get"]
        first["parameters"].append(
            {"name": "X-Required", "in": "header", "required": True, "schema": {"type": "string"}}
        )
        security, definitions, _warnings = select_security(document)
        operations, warnings = collect_operations(document, security)
        self.assertEqual(definitions[security.td_name]["scheme"], "apikey")
        self.assertFalse(operations)
        self.assertTrue(any("required header" in warning for warning in warnings))

    def test_maps_basic_bearer_and_header_api_key_security(self) -> None:
        cases = {
            "basic": ({"type": "http", "scheme": "basic"}, "basic"),
            "bearer": ({"type": "http", "scheme": "bearer"}, "bearer"),
            "key": (
                {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
                "apikey",
            ),
        }
        for name, (definition, expected) in cases.items():
            with self.subTest(name=name):
                document = api_document()
                document["components"]["securitySchemes"] = {name: definition}
                document["security"] = [{name: []}]
                selected, definitions, _warnings = select_security(document)
                self.assertEqual(definitions[selected.td_name]["scheme"], expected)

    def test_selects_operation_security_and_keeps_public_operations_public(self) -> None:
        document = api_document()
        document["components"]["securitySchemes"] = {
            "petstore_auth": {"type": "oauth2", "flows": {}},
            "api_key": {"type": "apiKey", "in": "header", "name": "api_key"},
        }
        document["paths"]["/pet/{petId}"] = {
            "get": {
                "operationId": "getPetById",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
                "security": [{"api_key": []}, {"petstore_auth": ["read:pets"]}],
            }
        }
        document["paths"]["/store/inventory"] = {
            "get": {
                "operationId": "getInventory",
                "responses": {"200": {"description": "ok"}},
                "security": [{"api_key": []}],
            }
        }
        document["paths"]["/pet"] = {
            "post": {
                "operationId": "addPet",
                "responses": {"200": {"description": "ok"}},
                "security": [{"petstore_auth": ["write:pets"]}],
            }
        }

        parsed = parsed_api(document)
        self.assertEqual(parsed.security.source_name, "api_key")
        self.assertEqual(
            {operation.operation_id for operation in parsed.operations},
            {"getPet", "getPetById", "getInventory"},
        )
        td, warnings = compile_thing(
            source(),
            parsed,
            operation_groups(parsed.operations)[0],
            group_external_id(source().external_id, parsed.server_url, "all"),
        )
        self.assertEqual(
            td["actions"]["getPetById"]["forms"][0]["href"],
            "https://eu.api.example.test/v1/pet/{petId}",
        )
        self.assertEqual(
            td["actions"]["getPetById"]["forms"][0]["security"],
            [parsed.security.td_name],
        )
        self.assertEqual(td["actions"]["getPet"]["forms"][0]["security"], ["nosec_sc"])
        self.assertTrue(any("unsupported" in warning for warning in warnings))
        self.assertTrue(any("incompatible" in warning for warning in warnings))

    def test_external_references_are_omitted_with_a_warning(self) -> None:
        document = api_document()
        first = next(iter(document["paths"].values()))["get"]
        first["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "https://schemas.example.test/pet.json"
        }
        security, _definitions, _warnings = select_security(document)
        operations, warnings = collect_operations(document, security)
        self.assertFalse(operations)
        self.assertTrue(any("external references" in warning for warning in warnings))

    def test_unsupported_top_level_security_never_becomes_public(self) -> None:
        document = api_document()
        document["components"]["securitySchemes"] = {"oauth": {"type": "oauth2", "flows": {}}}
        document["security"] = [{"oauth": []}]
        security, _definitions, warnings = select_security(document)
        operations, operation_warnings = collect_operations(document, security)
        self.assertFalse(operations)
        self.assertTrue(any("unsupported" in warning for warning in warnings))
        self.assertTrue(any("incompatible" in warning for warning in operation_warnings))


class OpenApiDocsTestCase(unittest.TestCase):
    def test_documentation_accepts_quoted_configuration_keys_and_spaced_attributes(self) -> None:
        for markup in (
            '<script>SwaggerUIBundle({"url": "/v1/openapi.json"})</script>',
            "<script>SwaggerUIBundle({'url': '/v1/openapi.json'})</script>",
            '<link rel = "service-desc" href = "/v1/openapi.json">',
            '<link href = "/v1/openapi.json" rel = "service-desc">',
            '<redoc spec-url = "/v1/openapi.json"></redoc>',
        ):
            with self.subTest(markup=markup):
                payload = HttpPayload(
                    "https://api.example/docs", 200, "text/html", markup.encode(), {}
                )
                self.assertEqual(declared_spec_url(payload), "https://api.example/v1/openapi.json")

    def test_invalid_and_unrelated_links_do_not_hide_a_valid_specification(self) -> None:
        for invalid in (
            "https://api.example:bad/openapi.json",
            "https://[broken/openapi.json",
            "https://name:secret@api.example/openapi.json",
            "https://unrelated.example/openapi.json",
            "javascript:openapi.json",
        ):
            with self.subTest(invalid=invalid):
                markup = (
                    f'<link rel="service-desc" href="{invalid}">'
                    '<link rel="service-desc" href="/v1/openapi.json">'
                )
                payload = HttpPayload(
                    "https://api.example/docs", 200, "text/html", markup.encode(), {}
                )
                self.assertEqual(declared_spec_url(payload), "https://api.example/v1/openapi.json")


class OpenApiProviderTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_direct_document_detection_uses_final_url_without_returning_spec(self) -> None:
        body = json.dumps(api_document()).encode()
        client = AsyncMock()
        client.get.return_value = HttpPayload(
            url="https://docs.example.test/final.json",
            status=200,
            content_type="application/json",
            body=body,
            headers={},
        )
        context = DetectionContext(url="https://docs.example.test/spec.json", http=client)
        with patch(
            "wotbot.discovery.providers.openapi.resolve_public",
            new=AsyncMock(return_value=(None, ["203.0.113.1"])),
        ):
            detected = await OpenApiProvider().inspect_public(context)
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.external_id, "https://docs.example.test/final.json")
        self.assertNotIn("paths", json.dumps(detected.config))
        self.assertTrue(any("Detected OpenAPI" in item for item in context.evidence))
        client.get.assert_awaited_once()

    async def test_api_base_detection_preserves_prefix_and_uses_final_spec_url(self) -> None:
        for status, final_base in (
            (404, "https://api.example.test/api/v1"),
            (404, "https://api.example.test/api/v1/"),
            (405, "https://api.example.test/api/v1"),
            (200, "https://api.example.test/api/v1.0?view=summary#top"),
        ):
            with self.subTest(status=status, final_base=final_base):
                base_path = final_base.split("?", 1)[0].rstrip("/")
                spec_url = f"{base_path}/openapi.json"
                canonical_url = "https://api.example.test/specs/current.json"
                client = AsyncMock()
                client.get.side_effect = [
                    HttpPayload(final_base, status, "application/json", b'{"status":"ok"}', {}),
                    HttpPayload(
                        canonical_url,
                        200,
                        "application/json",
                        json.dumps(api_document()).encode(),
                        {},
                    ),
                ]
                context = DetectionContext(url="https://api.example.test/start", http=client)
                with patch("wotbot.discovery.providers.openapi.resolve_public", new=AsyncMock()):
                    detected = await OpenApiProvider().inspect_public(context)
                assert detected is not None
                self.assertEqual(detected.external_id, canonical_url)
                self.assertEqual(detected.config["url"], canonical_url)
                self.assertEqual(
                    [call.args[0] for call in client.get.await_args_list],
                    [context.url, spec_url],
                )

    async def test_swagger_fallback_validates_each_response_without_following_more_links(
        self,
    ) -> None:
        base = "https://api.example.test/v1"
        swagger = {
            "swagger": "2.0",
            "info": {"title": "Weather API", "version": "1"},
            "host": "api.example.test",
            "basePath": "/v1",
            "schemes": ["https"],
            "paths": {},
        }
        for status, body in (
            (404, b""),
            (200, b'{"status":"ok"}'),
            (200, b'<html><script>SwaggerUIBundle({url: "/other.json"})</script></html>'),
        ):
            with self.subTest(status=status, body=body):
                client = AsyncMock()
                client.get.side_effect = [
                    HttpPayload(base, 404, "text/plain", b"", {}),
                    HttpPayload(f"{base}/openapi.json", status, "text/html", body, {}),
                    HttpPayload(
                        f"{base}/swagger.json",
                        200,
                        "application/json",
                        json.dumps(swagger).encode(),
                        {},
                    ),
                ]
                with patch("wotbot.discovery.providers.openapi.resolve_public", new=AsyncMock()):
                    detected = await OpenApiProvider().inspect_public(
                        DetectionContext(url=base, http=client)
                    )
                assert detected is not None
                self.assertEqual(detected.external_id, f"{base}/swagger.json")
                self.assertEqual(
                    [call.args[0] for call in client.get.await_args_list],
                    [base, f"{base}/openapi.json", f"{base}/swagger.json"],
                )

    async def test_missing_conventional_specs_stop_after_two_extra_probes(self) -> None:
        base = "https://api.example.test/v1"
        urls = [base, f"{base}/openapi.json", f"{base}/swagger.json"]
        client = AsyncMock()
        client.get.side_effect = [HttpPayload(url, 404, "text/plain", b"", {}) for url in urls]
        detected = await OpenApiProvider().inspect_public(DetectionContext(url=base, http=client))
        self.assertIsNone(detected)
        self.assertEqual([call.args[0] for call in client.get.await_args_list], urls)

    async def test_explicit_documents_and_other_http_errors_do_not_probe_base_paths(self) -> None:
        cases = [(status, "/v1") for status in (401, 403, 429, 500, 503)]
        cases += [
            (status, f"/spec.{extension}")
            for status in (200, 404)
            for extension in ("json", "yaml", "yml", "html", "rdf")
        ]
        for status, path in cases:
            with self.subTest(status=status, path=path):
                url = f"https://api.example.test{path}"
                client = AsyncMock()
                client.get.return_value = HttpPayload(url, status, "text/plain", b"missing", {})
                detected = await OpenApiProvider().inspect_public(
                    DetectionContext(url=url, http=client)
                )
                self.assertIsNone(detected)
                client.get.assert_awaited_once()

    async def test_empty_query_browses_groups_and_candidate_is_bounded(self) -> None:
        provider = OpenApiProvider()
        parsed = parsed_api(api_document(operation_count=31))
        with patch.object(provider, "_load", new=AsyncMock(return_value=parsed)):
            candidates = await provider.search(source(), SearchIntent(original=""), 10)
            result = await provider.onboarding_document(
                source(), candidates[0], runtime=AsyncMock()
            )
        self.assertEqual(len(candidates), 2)
        self.assertGreater(candidates[0].payload["compiler_version"], 2)
        self.assertEqual(
            result.document["wotbot:generation"]["compilerVersion"],
            candidates[0].payload["compiler_version"],
        )
        serialized = json.dumps([candidate.payload for candidate in candidates])
        self.assertNotIn('"paths"', serialized)
        self.assertIn("spec_digest", serialized)

    async def test_onboarding_rejects_stale_digest(self) -> None:
        provider = OpenApiProvider()
        parsed = parsed_api()
        candidate = CandidateDraft(
            provider="openapi",
            source_id=source().id,
            external_id=group_external_id(source().external_id, parsed.server_url, "all"),
            kind="api-service",
            title="Pet API",
            payload={"group_key": "all", "spec_digest": "old"},
        )
        with (
            patch.object(provider, "_load", new=AsyncMock(return_value=parsed)),
            self.assertRaisesRegex(StaleCandidateError, "changed"),
        ):
            await provider.onboarding_document(source(), candidate, runtime=AsyncMock())


class OpenApiRefreshMergeTestCase(unittest.TestCase):
    def test_generic_update_cannot_change_generated_action_contract(self) -> None:
        parsed = parsed_api()
        group = operation_groups(parsed.operations)[0]
        external_id = group_external_id(source().external_id, parsed.server_url, group.key)
        current, _ = compile_thing(source(), parsed, group, external_id)
        replacement = deepcopy(current)
        replacement["actions"]["getPet"]["uriVariables"]["petId"]["type"] = "number"

        with self.assertRaises(HTTPException) as raised:
            _validate_protected_resource_update(current, replacement, provider="openapi")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "OpenAPI-generated actions are protected")

    def test_generic_update_can_add_an_unmarked_manual_action(self) -> None:
        parsed = parsed_api()
        group = operation_groups(parsed.operations)[0]
        external_id = group_external_id(source().external_id, parsed.server_url, group.key)
        current, _ = compile_thing(source(), parsed, group, external_id)
        replacement = deepcopy(current)
        replacement["actions"]["manualAction"] = {
            "forms": [{"href": "https://manual.example.test/run"}]
        }

        _validate_protected_resource_update(current, replacement, provider="openapi")

    def test_refresh_preserves_local_metadata_and_manual_affordances(self) -> None:
        first = parsed_api()
        group = operation_groups(first.operations)[0]
        external_id = group_external_id(source().external_id, first.server_url, group.key)
        current, _ = compile_thing(source(), first, group, external_id)
        current["title"] = "My local title"
        current["description"] = "My local description"
        current["properties"] = {"manual": {"type": "string"}}
        current["actions"]["manualAction"] = {"forms": [{"href": "https://manual.example.test"}]}

        updated_spec = api_document()
        operation = next(iter(updated_spec["paths"].values()))["get"]
        operation["summary"] = "Updated summary"
        second = parsed_api(updated_spec)
        replacement, _ = compile_thing(
            source(), second, operation_groups(second.operations)[0], external_id
        )
        provider = OpenApiProvider()
        merged, removed_credentials = provider.merge_refresh(current, replacement)
        diff = provider.refresh_diff(current, merged)

        self.assertEqual(merged["title"], "My local title")
        self.assertIn("manual", merged["properties"])
        self.assertIn("manualAction", merged["actions"])
        self.assertEqual(diff["changed_actions"], ["getPet"])
        self.assertFalse(removed_credentials)


class RefreshStoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_preview_is_expiring_user_bound_and_single_use(self) -> None:
        class Redis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.expiry = 0

            async def set(self, key: str, value: str, *, ex: int) -> None:
                self.values[key] = value
                self.expiry = ex

            async def get(self, key: str) -> str | None:
                return self.values.get(key)

            async def delete(self, key: str) -> None:
                self.values.pop(key, None)

            async def aclose(self) -> None:
                return None

        redis = Redis()
        store = RefreshStore("redis://unused", ttl_seconds=600)
        record = RefreshRecord(
            user_id="user-a",
            thing_id="thing-a",
            source_id="source-a",
            provider="openapi",
            thing_document_hash="thing-hash",
            source_hash="source-hash",
            document={"id": "thing-a"},
        )
        with patch("wotbot.discovery.store.redis.from_url", return_value=redis):
            refresh_id = await store.put(record)
            self.assertEqual(redis.expiry, 600)
            self.assertEqual(await store.get(refresh_id, user_id="user-a"), record)
            with self.assertRaisesRegex(ValueError, "different user"):
                await store.get(refresh_id, user_id="user-b")
            await store.delete(refresh_id)
            with self.assertRaisesRegex(ValueError, "expired"):
                await store.get(refresh_id, user_id="user-a")
