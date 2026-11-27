"""The OGC SensorThings API provider against a scripted service."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException

from wotbot.catalog.service import _validate_protected_resource_update
from wotbot.catalog.validation import validate_document
from wotbot.discovery.detection import DetectionContext
from wotbot.discovery.errors import (
    SourceAuthenticationError,
    SourceProtocolError,
    StaleCandidateError,
)
from wotbot.discovery.http import HttpPayload
from wotbot.discovery.models import CandidateDraft, SearchIntent, SourceDefinition
from wotbot.discovery.providers import PROVIDERS
from wotbot.discovery.providers.public import detectors
from wotbot.discovery.providers.sensorthings import (
    SensorThingsProvider,
    entity_key,
    mqtt_broker,
    property_slug,
)
from wotbot.discovery.service import _provider_action_target

BASE = "https://sta.example/v1.1"
Response = tuple[int, Any]


class FakeClient:
    """Answers each GET through a handler and records the decoded requests."""

    def __init__(self, handler: Callable[[str, dict[str, str]], Response]) -> None:
        self.handler = handler
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []

    async def get(
        self, url: str, *, headers=None, max_bytes=None, credentialed=False
    ) -> HttpPayload:
        parts = urlsplit(url)
        path = f"{parts.scheme}://{parts.netloc}{parts.path}"
        params = dict(parse_qsl(parts.query))
        self.requests.append((path, params, dict(headers or {})))
        status, body = self.handler(path, params)
        return HttpPayload(
            url=url,
            status=status,
            content_type="application/json",
            body=json.dumps(body).encode(),
            headers={},
        )


def routes(table: dict[str, Any]) -> Callable[[str, dict[str, str]], Response]:
    def handler(path: str, _params: dict[str, str]) -> Response:
        if path not in table:
            return 404, {"code": 404, "message": "Not Found"}
        value = table[path]
        return value if isinstance(value, tuple) else (200, value)

    return handler


def root_document(base: str = BASE) -> dict[str, Any]:
    return {
        "value": [
            {"name": name, "url": f"{base}/{name}"}
            for name in ("Datastreams", "Locations", "Observations", "Things")
        ]
    }


def source(**overrides: Any) -> SourceDefinition:
    values: dict[str, Any] = {
        "id": "urn:source:sta",
        "provider": "sensorthings",
        "title": "SensorThings API sta.example",
        "external_id": BASE,
        "config": {"url": BASE},
        **overrides,
    }
    return SourceDefinition(**values)


def datastream(identifier: Any, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "@iot.id": identifier,
        "name": name,
        "description": "Vehicles counted by an infrared detector",
        "unitOfMeasurement": {"name": "Anzahl", "symbol": "", "definition": ""},
        "observationType": "http://www.opengis.net/def/ogc-om/OM_CountObservation",
        "phenomenonTime": "2026-02-28T00:00:00Z/2026-11-27T06:59:59Z",
        "ObservedProperty": {"name": "Anzahl Kfz je Zählstelle"},
        **extra,
    }


COUNTER = {
    "@iot.id": 12074,
    "name": "Verkehrszählstelle 0242982",
    "description": "Verkehrszählstelle zur Zählung von Mobilitätswerkzeugen",
    "properties": {"richtung": "West nach Ost", "keywords": ["Verkehrsmenge"]},
    "Datastreams": [
        datastream(31172, "Kfz-Aufkommen an Verkehrszählstelle 0242982 im 5-Min-Intervall"),
        datastream(30835, "Kfz-Aufkommen an Verkehrszählstelle 0242982 im 1-Tag-Intervall"),
    ],
    "Locations": [
        {
            "location": {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [10.027394, 53.600057]},
            }
        }
    ],
}


def candidate(key: str = "12074") -> CandidateDraft:
    return CandidateDraft(
        provider="sensorthings",
        source_id="urn:source:sta",
        external_id=key,
        kind="sensor-thing",
        title="Verkehrszählstelle 0242982",
    )


class SensorThingsDetectionTest(unittest.IsolatedAsyncioTestCase):
    async def detect(self, url: str, table: dict[str, Any]) -> tuple[Any, FakeClient, list[str]]:
        client = FakeClient(routes(table))
        context = DetectionContext(url=url, http=client)  # type: ignore[arg-type]
        return await SensorThingsProvider().inspect_public(context), client, context.evidence

    async def test_detects_the_service_root_below_a_bare_origin(self) -> None:
        detected, client, _evidence = await self.detect(
            "https://sta.example/",
            {"https://sta.example/v1.0": root_document("https://sta.example/v1.0")},
        )
        assert detected is not None
        self.assertEqual(detected.config, {"url": "https://sta.example/v1.0"})
        self.assertEqual(detected.provider, "sensorthings")
        self.assertEqual(
            [path for path, _params, _headers in client.requests],
            ["https://sta.example/v1.1", "https://sta.example/v1.0"],
        )

    async def test_uses_the_version_prefix_of_a_deeper_url(self) -> None:
        detected, client, _evidence = await self.detect(
            "https://sta.example/FROST-Server/v1.1/Things(3)?$expand=Datastreams",
            {
                "https://sta.example/FROST-Server/v1.1": root_document(
                    "https://sta.example/FROST-Server/v1.1"
                )
            },
        )
        assert detected is not None
        self.assertEqual(detected.external_id, "https://sta.example/FROST-Server/v1.1")
        self.assertEqual(len(client.requests), 1)

    async def test_does_not_probe_or_accept_other_services(self) -> None:
        _detected, client, evidence = await self.detect("https://portal.example/datasets", {})
        self.assertEqual(client.requests, [])
        self.assertTrue(evidence)
        for name, document in {
            "foreign collections": root_document("https://elsewhere.example/v1.1"),
            "no datastreams": {"value": [{"name": "Things", "url": f"{BASE}/Things"}]},
            "openapi": {"openapi": "3.0.0"},
        }.items():
            with self.subTest(name):
                detected, _client, _evidence = await self.detect(BASE, {BASE: document})
                self.assertIsNone(detected)

    def test_probes_before_openapi(self) -> None:
        order = [provider.name for provider in detectors(PROVIDERS)]
        self.assertLess(order.index("sensorthings"), order.index("openapi"))


class SensorThingsSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_filters_on_the_server_and_ranks_the_window(self) -> None:
        listing = {
            "value": [
                {"@iot.id": 9, "name": "E-Ladestation", "Datastreams": [{"name": "Ladepunkt 1"}]},
                {**COUNTER, "Datastreams": [{"name": "Kfz 5-Min"}, {"name": "Kfz 1-Tag"}]},
                {"name": "no identity"},
            ]
        }
        client = FakeClient(routes({f"{BASE}/Things": listing}))
        found = await SensorThingsProvider().search(
            source(),
            SearchIntent(
                original="Verkehrszählstelle Straße", keywords=("Verkehrszählstelle", "Straße")
            ),
            10,
            public_http=client,  # type: ignore[arg-type]
        )

        [(_path, params, _headers)] = client.requests
        # tolower() on the server keeps ß, so the term must not be casefolded.
        self.assertEqual(
            params["$filter"],
            "(substringof('verkehrszählstelle',tolower(name)) or "
            "substringof('verkehrszählstelle',tolower(description))) and "
            "(substringof('straße',tolower(name)) or "
            "substringof('straße',tolower(description)))",
        )
        self.assertEqual(params["$orderby"], "@iot.id desc")
        self.assertEqual([item.external_id for item in found], ["12074", "9"])
        self.assertIn("Datastreams: Kfz 5-Min; Kfz 1-Tag", found[0].summary)
        self.assertEqual(params["$expand"], "Datastreams($select=name;$top=21)")
        self.assertEqual(found[0].links[0]["url"], f"{BASE}/Things(12074)")

    async def test_summary_marks_a_cut_off_datastream_list(self) -> None:
        crowded = {**COUNTER, "Datastreams": [{"name": f"D{index}"} for index in range(21)]}
        client = FakeClient(routes({f"{BASE}/Things": {"value": [crowded]}}))
        [found] = await SensorThingsProvider().search(
            source(),
            SearchIntent(original=""),
            10,
            public_http=client,  # type: ignore[arg-type]
        )
        self.assertTrue(found.summary.endswith("D4 (+at least 16 more)"), found.summary)

    async def test_falls_back_to_any_term_and_escapes_quotes(self) -> None:
        def handler(_path: str, params: dict[str, str]) -> Response:
            matched = " or (" in params["$filter"]
            return 200, {"value": [COUNTER] if matched else []}

        client = FakeClient(handler)
        found = await SensorThingsProvider().search(
            source(),
            SearchIntent(original="O'Brien Zählstelle", keywords=("O'Brien", "Zählstelle")),
            10,
            public_http=client,  # type: ignore[arg-type]
        )
        self.assertEqual([item.external_id for item in found], ["12074"])
        self.assertEqual(len(client.requests), 2)
        self.assertIn("substringof('o''brien',tolower(name))", client.requests[0][1]["$filter"])

    async def test_lists_newest_things_without_terms_and_reports_credentials(self) -> None:
        client = FakeClient(routes({f"{BASE}/Things": {"value": [COUNTER]}}))
        await SensorThingsProvider().search(
            source(),
            SearchIntent(original=""),
            10,
            public_http=client,  # type: ignore[arg-type]
        )
        self.assertNotIn("$filter", client.requests[0][1])

        rejected = FakeClient(routes({f"{BASE}/Things": (401, {})}))
        with self.assertRaises(SourceAuthenticationError):
            await SensorThingsProvider().search(
                source(security_scheme="bearer", credential={"token": "secret"}),
                SearchIntent(original=""),
                10,
                public_http=rejected,  # type: ignore[arg-type]
            )
        self.assertEqual(rejected.requests[0][2]["Authorization"], "Bearer secret")


def mqtt_root(*endpoints: Any) -> dict[str, Any]:
    return {
        **root_document(),
        "serverSettings": {
            "conformance": [],
            "http://www.opengis.net/spec/iot_sensing/1.1/req/receive-updates-via-mqtt/"
            "receive-updates": {"endpoints": list(endpoints)},
        },
    }


class SensorThingsOnboardingTest(unittest.IsolatedAsyncioTestCase):
    async def onboard(self, thing: Any, *, root: Any = None, **source_overrides: Any):
        status = 404 if thing is None else 200
        table: dict[str, Any] = {f"{BASE}/Things(12074)": (status, thing)}
        if root is not None:
            table[BASE] = root
        self.client = FakeClient(routes(table))
        with patch(
            "wotbot.discovery.providers.sensorthings.source_client", return_value=self.client
        ):
            return await SensorThingsProvider().onboarding_document(
                source(**source_overrides), candidate(), runtime=None
            )

    async def test_advertised_mqtt_updates_become_observation_events(self) -> None:
        result = await self.onboard(COUNTER, root=mqtt_root("mqtt://sta.example:1883"))
        document = validate_document(result.document)

        self.assertEqual(result.warnings, ())
        self.assertIn({"mqv": "https://www.w3.org/2019/wot/mqtt#"}, document["@context"])
        event = document["events"]["kfz_aufkommen_an_im_5_min_intervall_observation"]
        self.assertEqual(
            event["forms"],
            [
                {
                    "href": "mqtt://sta.example:1883/v1.1/Datastreams(31172)/Observations",
                    "op": ["subscribeevent", "unsubscribeevent"],
                    "contentType": "application/json",
                    "mqv:filter": "v1.1/Datastreams(31172)/Observations",
                }
            ],
        )
        self.assertEqual(event["data"]["properties"]["result"]["unit"], "Anzahl")
        self.assertEqual(len(document["events"]), 2)

    async def test_events_need_the_services_own_broker_and_no_credential(self) -> None:
        for name, root in {
            "no settings": root_document(),
            "foreign broker": mqtt_root("mqtt://broker.elsewhere.example:1883"),
            "not mqtt": mqtt_root("https://sta.example/mqtt"),
        }.items():
            with self.subTest(name):
                result = await self.onboard(COUNTER, root=root)
                self.assertNotIn("events", result.document)
                self.assertNotIn("mqv", json.dumps(result.document["@context"]))

        result = await self.onboard(
            COUNTER,
            root=mqtt_root("mqtt://sta.example:1883"),
            security_scheme="apikey",
            credential={"apiKey": "secret"},
        )
        self.assertNotIn("events", result.document)
        self.assertEqual(len(self.client.requests), 1)

    async def test_skips_events_whose_topic_would_not_survive_the_href(self) -> None:
        thing = {**COUNTER, "Datastreams": [datastream("a b", "Count"), datastream("c#", "X")]}
        result = await self.onboard(thing, root=mqtt_root("mqtts://sta.example"))
        self.assertNotIn("events", result.document)
        self.assertEqual(len(result.warnings), 2)
        self.assertIn("no usable MQTT topic", result.warnings[0])

    async def test_compiles_datastreams_into_latest_properties_and_history_actions(
        self,
    ) -> None:
        result = await self.onboard(COUNTER)
        document = validate_document(result.document)

        self.assertEqual(result.warnings, ())
        self.assertTrue(document["id"].startswith("urn:wotbot:external:sensorthings:"))
        self.assertEqual(
            list(document["properties"]),
            ["kfz_aufkommen_an_im_5_min_intervall", "kfz_aufkommen_an_im_1_tag_intervall"],
        )
        latest = document["properties"]["kfz_aufkommen_an_im_5_min_intervall"]
        self.assertEqual(
            latest["forms"][0]["href"],
            f"{BASE}/Datastreams(31172)/Observations"
            "?$top=1&$orderby=phenomenonTime%20desc&$select=phenomenonTime,result",
        )
        result_schema = latest["properties"]["value"]["items"]["properties"]["result"]
        self.assertEqual(result_schema["unit"], "Anzahl")
        self.assertEqual(document["wotbot:location"]["type"], "Point")
        self.assertEqual(document["wotbot:sensorThingsProperties"]["richtung"], "West nach Ost")

        target = _provider_action_target(
            document,
            thing_id=document["id"],
            action="kfz_aufkommen_an_im_5_min_intervall_history",
            provider="sensorthings",
        )
        self.assertEqual(target["path"], "/Datastreams(31172)/Observations")
        self.assertEqual(target["query_variables"], ("from", "to", "limit"))

    async def test_generated_forms_are_protected(self) -> None:
        result = await self.onboard(COUNTER, root=mqtt_root("mqtt://sta.example:1883"))
        document = validate_document(result.document)
        name = "kfz_aufkommen_an_im_5_min_intervall"
        for kind, affordance, field, value in (
            ("actions", f"{name}_history", "wotbot:path", "/Things(1)/Observations"),
            ("properties", name, "href", "http://internal.example/secret"),
            ("events", f"{name}_observation", "href", "mqtt://broker.elsewhere.example/x"),
        ):
            with self.subTest(kind):
                edited = json.loads(json.dumps(document))
                edited[kind][affordance]["forms"][0][field] = value
                with self.assertRaises(HTTPException) as caught:
                    _validate_protected_resource_update(document, edited, provider="sensorthings")
                self.assertEqual(caught.exception.status_code, 409)

    async def test_credentialed_sources_get_history_only(self) -> None:
        result = await self.onboard(
            COUNTER, security_scheme="apikey", credential={"apiKey": "secret"}
        )
        self.assertNotIn("properties", result.document)
        self.assertEqual(len(result.document["actions"]), 2)
        self.assertIn("limit=1", result.warnings[0])

    async def test_bounds_datastreams_and_keeps_names_unique(self) -> None:
        many = {
            **COUNTER,
            "Datastreams": [datastream(index, "Count") for index in range(1, 53)],
        }
        result = await self.onboard(many)
        self.assertEqual(len(result.document["actions"]), 50)
        self.assertIn("count_2_history", result.document["actions"])
        self.assertIn("first 50", result.warnings[0])

        crowded = {
            **COUNTER,
            "Datastreams": [
                datastream(7, "Temp 5"),
                datastream(3, "Temp"),
                datastream(5, "Temp"),
            ],
        }
        result = await self.onboard(crowded)
        self.assertEqual(list(result.document["properties"]), ["temp_5", "temp", "temp_5_2"])

    async def test_string_keys_are_encoded_in_every_url(self) -> None:
        thing = {**COUNTER, "@iot.id": "a#b", "Datastreams": [datastream("x?y", "Count")]}
        client = FakeClient(routes({f"{BASE}/Things('a%23b')": thing}))
        with patch("wotbot.discovery.providers.sensorthings.source_client", return_value=client):
            result = await SensorThingsProvider().onboarding_document(
                source(), candidate("'a#b'"), runtime=None
            )
        document = validate_document(result.document)
        self.assertEqual(document["links"][0]["href"], f"{BASE}/Things('a%23b')")
        self.assertTrue(
            document["properties"]["count"]["forms"][0]["href"].startswith(
                f"{BASE}/Datastreams('x%3Fy')/Observations?"
            )
        )
        target = _provider_action_target(
            document, thing_id=document["id"], action="count_history", provider="sensorthings"
        )
        self.assertEqual(target["path"], "/Datastreams('x%3Fy')/Observations")

    async def test_refuses_stale_or_substituted_things(self) -> None:
        with self.assertRaises(StaleCandidateError):
            await self.onboard(None)
        with self.assertRaises(SourceProtocolError):
            await self.onboard({**COUNTER, "@iot.id": 1})
        with self.assertRaises(SourceProtocolError):
            await SensorThingsProvider().onboarding_document(
                source(), candidate("1) or (2"), runtime=None
            )

    def test_broker_must_be_a_bare_endpoint_on_the_service_host(self) -> None:
        self.assertEqual(
            mqtt_broker(mqtt_root("mqtt://[fd00::1]:1883"), "http://[fd00::1]:8080/v1.1"),
            "mqtt://[fd00::1]:1883",
        )
        self.assertEqual(mqtt_broker(mqtt_root("mqtts://STA.example"), BASE), "mqtts://sta.example")
        for endpoint in (
            "mqtt://user:pw@sta.example:1883",
            "mqtt://sta.example:1883/prefix",
            "mqtt://sta.example:99999",
            7,
        ):
            with self.subTest(endpoint):
                self.assertIsNone(mqtt_broker(mqtt_root(endpoint), BASE))

    def test_names_and_keys(self) -> None:
        self.assertEqual(entity_key(7), "7")
        self.assertEqual(entity_key("it's"), "'it''s'")
        self.assertEqual(property_slug("Thing A temperature", "Thing A"), "temperature")
        self.assertEqual(property_slug("Thing A", "Thing A"), "thing_a")


def observations(start: int, count: int) -> list[dict[str, Any]]:
    return [
        {"phenomenonTime": f"2026-11-26T{hour:02d}:00:00Z", "result": hour}
        for hour in range(start, start - count, -1)
    ]


class SensorThingsHistoryTest(unittest.IsolatedAsyncioTestCase):
    PATH = "/Datastreams(31172)/Observations"

    async def history(
        self,
        handler: Callable[[str, dict[str, str]], Response],
        *,
        uri_variables: dict[str, Any] | None = None,
        input_data: Any = None,
        path: str = PATH,
        method: str = "GET",
    ) -> tuple[dict[str, Any], FakeClient]:
        client = FakeClient(handler)
        response = await SensorThingsProvider().invoke_api(
            source(),
            external_id="12074",
            method=method,
            path=path,
            path_variables=(),
            query_variables=("from", "to", "limit"),
            uri_variables=uri_variables or {},
            input_data=input_data,
            public_http=client,  # type: ignore[arg-type]
        )
        return json.loads(response.body), client

    async def test_pages_newest_first_and_returns_chronological_order(self) -> None:
        def handler(_path: str, params: dict[str, str]) -> Response:
            if "$skip" in params:
                return 200, {"value": observations(20, 3)}
            return 200, {
                "value": observations(23, 3),
                "@iot.nextLink": f"{BASE}{self.PATH}?$top=3&$skip=3",
            }

        body, client = await self.history(
            handler,
            uri_variables={"from": "2026-11-26", "to": "2026-11-26T23:59:59+01:00", "limit": "5"},
        )
        params = client.requests[0][1]
        self.assertEqual(
            params["$filter"],
            "phenomenonTime ge 2026-11-26T00:00:00.000Z and "
            "phenomenonTime le 2026-11-26T22:59:59.000Z",
        )
        self.assertEqual(params["$orderby"], "phenomenonTime desc")
        self.assertEqual(params["$top"], "5")
        self.assertEqual(body["count"], 5)
        self.assertTrue(body["truncated"])
        self.assertEqual([item["result"] for item in body["observations"]], [19, 20, 21, 22, 23])

    async def test_reads_variables_from_input_and_reports_complete_windows(self) -> None:
        body, client = await self.history(
            lambda _path, _params: (200, {"value": observations(2, 2)}),
            input_data={"limit": 10, "unrelated": True},
        )
        self.assertNotIn("$filter", client.requests[0][1])
        self.assertEqual(client.requests[0][1]["$top"], "10")
        self.assertEqual(
            body,
            {
                "count": 2,
                "truncated": False,
                "observations": [
                    {"phenomenonTime": "2026-11-26T01:00:00Z", "result": 1},
                    {"phenomenonTime": "2026-11-26T02:00:00Z", "result": 2},
                ],
            },
        )

    async def test_pagination_is_rebased_onto_the_registered_root(self) -> None:
        def handler(_path: str, params: dict[str, str]) -> Response:
            if "$skip" in params:
                return 200, {"value": observations(22, 1)}
            return 200, {
                "value": observations(23, 1),
                # A server behind a TLS proxy may call itself http:// on an internal host.
                "@iot.nextLink": f"http://internal:8080/v1.1{self.PATH}?$skip=1",
            }

        body, client = await self.history(handler, uri_variables={"limit": 2})
        self.assertEqual(body["count"], 2)
        self.assertEqual(client.requests[1][0], f"{BASE}{self.PATH}")

    async def test_refuses_pagination_that_leaves_the_service_root(self) -> None:
        for link in ("https://sta.example/v1.0/Observations", 7):

            def handler(_path: str, _params: dict[str, str], link: Any = link) -> Response:
                return 200, {"value": observations(23, 1), "@iot.nextLink": link}

            with self.subTest(link), self.assertRaises(SourceProtocolError):
                await self.history(handler)

    async def test_date_only_to_covers_the_whole_day(self) -> None:
        _body, client = await self.history(
            lambda _path, _params: (200, {"value": []}),
            uri_variables={"from": "0001-01-01", "to": "2026-11-26"},
        )
        self.assertEqual(
            client.requests[0][1]["$filter"],
            "phenomenonTime ge 0001-01-01T00:00:00.000Z and "
            "phenomenonTime le 2026-11-26T23:59:59.999Z",
        )

    async def test_rejects_invalid_requests_before_fetching(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "other path": {"path": "/Things(1)"},
            "write": {"method": "POST"},
            "bad from": {"uri_variables": {"from": "yesterday"}},
            "reversed": {"uri_variables": {"from": "2026-11-27", "to": "2026-11-26"}},
            "limit too high": {"uri_variables": {"limit": 5001}},
            "limit not integer": {"uri_variables": {"limit": "5.5"}},
            "limit bool": {"uri_variables": {"limit": True}},
        }
        for name, arguments in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                await self.history(lambda _path, _params: (200, {"value": []}), **arguments)


if __name__ == "__main__":
    unittest.main()
