"""The WoT Thing Description Directory provider against a scripted directory."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from wotbot.catalog.validation import validate_document
from wotbot.discovery.detection import DetectionContext
from wotbot.discovery.errors import (
    SourceAuthenticationError,
    SourceProtocolError,
    StaleCandidateError,
)
from wotbot.discovery.http import HttpPayload
from wotbot.discovery.models import SearchIntent, SourceDefinition
from wotbot.discovery.providers import PROVIDERS
from wotbot.discovery.providers.wot_tdd import WotTddProvider, directory_base

BASE = "http://directory:8081"


def td(thing_id: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "@context": [
            "https://www.w3.org/2022/wot/td/v1.1",
            {"qudt": "http://qudt.org/schema/qudt/"},
        ],
        "id": thing_id,
        "title": title,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        **extra,
    }


STATION = td(
    "urn:test:station",
    "Dudopark Ortsnetzstation",
    description="Substation of the Dudopark district",
    properties={
        "power": {
            "type": "number",
            "description": "Active power; negative values mean reverse feed-in",
            "qudt:unit": {"@id": "http://qudt.org/vocab/unit/W"},
            "forms": [{"href": "http://participant:9000/api/history/station/power/latest"}],
        }
    },
)
METER = td("urn:test:meter", "Smart meter")


class FakeClient:
    """Serves scripted responses by exact URL and records what was fetched."""

    def __init__(self, routes: dict[str, tuple[int, Any, dict[str, str]]]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, headers=None, max_bytes=None, credentialed=False
    ) -> HttpPayload:
        self.requests.append((url, dict(headers or {})))
        status, body, response_headers = self.routes.get(url, (404, {"detail": "missing"}, {}))
        return HttpPayload(
            url=url,
            status=status,
            content_type="application/td+json",
            body=json.dumps(body).encode(),
            headers=response_headers,
        )


def source(**overrides: Any) -> SourceDefinition:
    values: dict[str, Any] = {
        "id": "urn:source:tdd",
        "provider": "wot-tdd",
        "title": "Local Energy Data Provider",
        "external_id": BASE,
        "network_access": "private",
        "config": {"url": BASE},
        **overrides,
    }
    return SourceDefinition(**values)


def listing(
    *pages: tuple[str, list[Any], str | None],
) -> dict[str, tuple[int, Any, dict[str, str]]]:
    routes = {}
    for url, body, next_url in pages:
        headers = {"link": f'<{next_url}>; rel="next"'} if next_url else {}
        routes[url] = (200, body, headers)
    return routes


FIRST_PAGE = f"{BASE}/things?offset=0&limit=100"


class WotTddSearchTest(unittest.IsolatedAsyncioTestCase):
    def patched(self, client: FakeClient):
        return patch("wotbot.discovery.providers.wot_tdd.source_client", return_value=client)

    async def test_pages_ranks_and_onboards_the_published_td(self) -> None:
        enriched = {**STATION, "registration": {"created": "2026-09-27T08:00:00Z"}}
        anonymous = {key: value for key, value in METER.items() if key != "id"}
        client = FakeClient(
            {
                **listing(
                    (FIRST_PAGE, [METER, anonymous], "/things?offset=2&limit=100"),
                    (f"{BASE}/things?offset=2&limit=100", [enriched], None),
                ),
                f"{BASE}/things/urn%3Atest%3Astation": (200, enriched, {}),
            }
        )
        provider = WotTddProvider()
        with self.patched(client):
            candidates = await provider.search(
                source(),
                SearchIntent(original="reverse feed-in", keywords=("reverse", "feed-in")),
                10,
            )
            self.assertEqual(
                [item.external_id for item in candidates], ["urn:test:station", "urn:test:meter"]
            )
            self.assertEqual(candidates[0].kind, "thing-description")
            self.assertEqual(candidates[0].summary, "Substation of the Dudopark district")
            self.assertEqual(candidates[0].links[0]["url"], f"{BASE}/things/urn%3Atest%3Astation")
            result = await provider.onboarding_document(source(), candidates[0], runtime=None)

        self.assertEqual(result.document, STATION)
        validate_document(result.document)
        self.assertTrue(
            all("application/td+json" in headers["Accept"] for _url, headers in client.requests)
        )

    async def test_accepts_the_draft_collection_format(self) -> None:
        client = FakeClient(listing((FIRST_PAGE, {"members": [STATION]}, None)))
        with self.patched(client):
            candidates = await WotTddProvider().search(source(), SearchIntent(original=""), 10)
        self.assertEqual([item.external_id for item in candidates], ["urn:test:station"])

    async def test_refuses_listings_it_cannot_rank_faithfully(self) -> None:
        cases = {
            "duplicate": listing((FIRST_PAGE, [STATION, STATION], None)),
            "not an array": listing((FIRST_PAGE, {"things": []}, None)),
            "cross-origin next": listing(
                (FIRST_PAGE, [STATION], "http://elsewhere:8081/things?offset=1")
            ),
            "too large": listing(
                (
                    FIRST_PAGE,
                    [td(f"urn:test:{index}", f"Thing {index}") for index in range(501)],
                    None,
                )
            ),
        }
        for name, routes in cases.items():
            with self.subTest(name), self.patched(FakeClient(routes)):
                with self.assertRaises(SourceProtocolError):
                    await WotTddProvider().search(source(), SearchIntent(original=""), 10)

    async def test_rejected_credential_is_reported_as_such(self) -> None:
        client = FakeClient({FIRST_PAGE: (401, {}, {})})
        with self.patched(client), self.assertRaises(SourceAuthenticationError):
            await WotTddProvider().search(
                source(security_scheme="bearer", credential={"token": "secret"}),
                SearchIntent(original=""),
                10,
            )
        self.assertEqual(client.requests[0][1]["Authorization"], "Bearer secret")

    async def test_onboarding_requires_the_listed_identity(self) -> None:
        client = FakeClient(listing((FIRST_PAGE, [STATION], None)))
        provider = WotTddProvider()
        with self.patched(client):
            [candidate] = await provider.search(source(), SearchIntent(original=""), 10)
            client.routes[f"{BASE}/things/urn%3Atest%3Astation"] = (200, METER, {})
            with self.assertRaises(SourceProtocolError):
                await provider.onboarding_document(source(), candidate, runtime=None)
            del client.routes[f"{BASE}/things/urn%3Atest%3Astation"]
            with self.assertRaises(StaleCandidateError):
                await provider.onboarding_document(source(), candidate, runtime=None)


class WotTddDetectionTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def directory_td(href: str, **extra: Any) -> dict[str, Any]:
        return td(
            "urn:test:directory",
            "Participant directory",
            **{
                "@type": "tdd:ThingDirectory",
                "properties": {"things": {"type": "array", "forms": [{"href": href}]}},
                **extra,
            },
        )

    async def detect(self, document: Any) -> tuple[SourceDefinition | None, list[str]]:
        client = FakeClient({"https://tdd.example/.well-known/wot": (200, document, {})})
        context = DetectionContext(url="https://tdd.example/portal", http=client)  # type: ignore[arg-type]
        return await WotTddProvider().inspect_public(context), context.evidence

    async def test_detects_a_directory_from_its_introduction(self) -> None:
        detected, _evidence = await self.detect(self.directory_td("/api/things"))
        assert detected is not None
        self.assertEqual(detected.config, {"url": "https://tdd.example/api"})
        self.assertEqual(detected.provider, "wot-tdd")
        self.assertEqual(detected.title, "Participant directory")

    async def test_ignores_other_things_and_foreign_listings(self) -> None:
        for name, document in {
            "plain thing": STATION,
            "foreign listing": self.directory_td("https://elsewhere.example/things"),
            "no listing": self.directory_td("/search/jsonpath"),
        }.items():
            with self.subTest(name):
                detected, evidence = await self.detect(document)
                self.assertIsNone(detected)
                self.assertTrue(evidence)

    def test_base_resolves_relative_to_the_declared_base(self) -> None:
        document = self.directory_td("things{?offset,limit}", base="https://tdd.example/v1/")
        document["properties"]["things"]["forms"][0]["href"] = "things?offset=0"
        self.assertEqual(
            directory_base(document, "https://tdd.example/.well-known/wot"),
            "https://tdd.example/v1",
        )

    def test_registered_and_detectable(self) -> None:
        provider = PROVIDERS["wot-tdd"]
        self.assertIn("detect", provider.capabilities)
        self.assertEqual(provider.normalize_config({"url": f"{BASE}/"}), {"url": BASE})


if __name__ == "__main__":
    unittest.main()
