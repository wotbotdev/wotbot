import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from wotbot.api.main import app
from wotbot.catalog.enrichment.models import (
    EnrichmentResult,
    EnrichmentValidation,
    ShaclFinding,
)
from wotbot.catalog.events import publish_pending_thing_events
from wotbot.catalog.service import ThingCatalogWriteService


class RecordingPublisher:
    def __init__(self):
        self.events: list[dict[str, object]] = []

    def publish(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def close(self) -> None:
        return None


class FailingPublisher:
    def publish(self, _event: dict[str, object]) -> None:
        raise RuntimeError("publisher unavailable")

    def close(self) -> None:
        return None


def sample_thing(thing_id: str = "urn:thing:alpha") -> dict[str, object]:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": "Alpha Sensor",
        "description": "Process temperature monitor for assembly line 3",
        "tags": ["line-3", "sensor"],
        "securityDefinitions": {
            "nosec_sc": {
                "scheme": "nosec",
            }
        },
        "security": "nosec_sc",
        "properties": {
            "temperature": {
                "type": "number",
                "description": "Ambient temperature",
                "forms": [
                    {
                        "href": "https://example.com/things/alpha/properties/temperature",
                    }
                ],
            }
        },
    }


def flush_outbox(client: TestClient, publisher: RecordingPublisher) -> int:
    return publish_pending_thing_events(client.app.state.session_factory, publisher)


def test_api_me_returns_authenticated_identity(authenticated_headers):
    with TestClient(app) as client:
        response = client.get("/api/me", headers=authenticated_headers)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "init-admin",
        "email": None,
        "preferred_username": None,
        "groups": [],
        "scopes": [
            "things:read",
            "things:write",
            "things:delete",
            "search:read",
            "credentials:read",
            "credentials:write",
            "keys:manage",
        ],
    }


def test_api_me_requires_authentication():
    with TestClient(app) as client:
        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_api_things_crud_and_events(authenticated_headers):
    thing = sample_thing()

    with TestClient(app) as client:
        publisher = RecordingPublisher()
        client.app.state.event_publisher = publisher

        create_response = client.post(
            "/api/things",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=thing,
        )
        assert create_response.status_code == 201
        flush_outbox(client, publisher)
        assert create_response.json()["id"] == thing["id"]
        assert create_response.json()["origin"] == {"kind": "manual"}
        assert publisher.events[-1]["eventType"] == "create"

        list_response = client.get(
            "/api/things?q=line-3",
            headers=authenticated_headers,
        )
        assert list_response.status_code == 200
        assert list_response.json()["total"] == 1
        assert list_response.json()["items"][0]["id"] == thing["id"]

        discovered_only = client.get(
            "/api/things?origin_kind=discovery",
            headers=authenticated_headers,
        )
        assert discovered_only.status_code == 200
        assert discovered_only.json()["total"] == 0

        get_response = client.get(
            f"/api/things/{thing['id']}",
            headers=authenticated_headers,
        )
        assert get_response.status_code == 200
        assert get_response.json()["document"]["properties"]["temperature"]["type"] == "number"

        updated = sample_thing()
        updated["description"] = "Updated process temperature monitor"
        update_response = client.put(
            f"/api/things/{thing['id']}",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=updated,
        )
        assert update_response.status_code == 200
        flush_outbox(client, publisher)
        assert update_response.json()["description"] == "Updated process temperature monitor"
        assert publisher.events[-1]["eventType"] == "update"

        delete_response = client.delete(
            f"/api/things/{thing['id']}",
            headers=authenticated_headers,
        )
        assert delete_response.status_code == 200
        flush_outbox(client, publisher)
        assert delete_response.json() == {"id": thing["id"], "status": "deleted"}
        assert publisher.events[-1] == {
            "eventType": "remove",
            "id": thing["id"],
        }


def test_discovered_origin_is_structured_idempotent_and_collision_safe(authenticated_headers):
    discovered = sample_thing("urn:thing:provider")
    manual = sample_thing("urn:thing:manual")

    with TestClient(app) as client:
        with client.app.state.session_factory() as session:
            service = ThingCatalogWriteService(session)
            first, first_created = service.create_discovered(
                discovered,
                provider="toolhive",
                source_id="local",
                external_id="weather",
            )
            second, second_created = service.create_discovered(
                discovered,
                provider="toolhive",
                source_id="local",
                external_id="weather",
            )
            assert first.id == second.id
            assert first_created is True
            assert second_created is False

            service.create(manual)
            with pytest.raises(HTTPException) as collision:
                service.create_discovered(
                    manual,
                    provider="toolhive",
                    source_id="local",
                    external_id="different",
                )
            assert collision.value.status_code == 409

        response = client.get(
            f"/api/things/{discovered['id']}",
            headers=authenticated_headers,
        )
        assert response.status_code == 200
        assert response.json()["origin"] == {
            "kind": "discovery",
            "provider": "toolhive",
            "source_id": "local",
            "external_id": "weather",
        }
        assert "origin" not in response.json()["document"]
        assert "source" not in response.json()["document"]


def test_api_things_rejects_invalid_document(authenticated_headers):
    invalid_thing = sample_thing("urn:thing:invalid")
    invalid_thing.pop("id")

    with TestClient(app) as client:
        response = client.post(
            "/api/things",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=invalid_thing,
        )

    assert response.status_code == 422
    assert "id" in response.json()["detail"]


def test_api_things_enrich_returns_proposal_without_catalog_event(
    authenticated_headers,
    monkeypatch,
):
    thing = sample_thing("urn:thing:enrich")
    calls: list[dict[str, object]] = []

    async def fake_enrich(document, **_kwargs):
        calls.append(document)
        enriched = {
            **document,
            "@context": [
                document["@context"],
                {"saref": "https://saref.etsi.org/core/"},
            ],
            "@type": "saref:TemperatureSensor",
        }
        return EnrichmentResult(
            enriched=enriched,
            diff=[
                {
                    "kind": "type",
                    "path": "@type",
                    "value": "saref:TemperatureSensor",
                    "label": "Thing type",
                }
            ],
            validation=EnrichmentValidation(
                ok=True,
                attempts=1,
                shacl_conforms=True,
                shacl_findings=[
                    ShaclFinding(
                        severity="http://www.w3.org/ns/shacl#Warning",
                        message="Advisory finding",
                    )
                ],
            ),
        )

    monkeypatch.setattr("wotbot.catalog.router.enrich_thing_document", fake_enrich)
    monkeypatch.setattr("wotbot.catalog.router.make_llm", lambda _settings: object())

    with TestClient(app) as client:
        publisher = RecordingPublisher()
        client.app.state.event_publisher = publisher
        response = client.post(
            f"/api/things/{thing['id']}/enrich",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json={"document": thing},
        )

    assert response.status_code == 200, response.text
    assert response.json()["enriched"]["@type"] == "saref:TemperatureSensor"
    assert response.json()["diff"][0]["kind"] == "type"
    assert response.json()["validation"]["shacl_conforms"] is True
    assert response.json()["validation"]["shacl_findings"][0]["message"] == "Advisory finding"
    assert calls == [thing]
    assert publisher.events == []


def test_api_things_enrich_rejects_missing_document(authenticated_headers):
    with TestClient(app) as client:
        response = client.post(
            "/api/things/urn:thing:missing/enrich",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json={},
        )

    assert response.status_code == 422
    assert "document object" in response.json()["detail"]


def test_api_things_write_succeeds_when_publisher_is_unavailable(authenticated_headers):
    thing = sample_thing("urn:thing:queued")

    with TestClient(app) as client:
        client.app.state.event_publisher = FailingPublisher()

        create_response = client.post(
            "/api/things",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=thing,
        )
        assert create_response.status_code == 201, create_response.text

        get_response = client.get(
            f"/api/things/{thing['id']}",
            headers=authenticated_headers,
        )
        assert get_response.status_code == 200
        assert get_response.json()["id"] == thing["id"]

        recovery_publisher = RecordingPublisher()
        published = flush_outbox(client, recovery_publisher)
        assert published == 1
        assert recovery_publisher.events[0]["id"] == thing["id"]
        assert recovery_publisher.events[0]["eventType"] == "create"
        assert isinstance(recovery_publisher.events[0]["hash"], str)
        assert recovery_publisher.events[0]["hash"]
