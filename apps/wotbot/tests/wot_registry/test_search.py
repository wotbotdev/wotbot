from fastapi.testclient import TestClient

from wotbot.api.main import app


def sample_thing(thing_id: str = "urn:thing:search-stub") -> dict[str, object]:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": "Alpha Sensor",
        "description": "Process temperature monitor for assembly line 3",
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


def test_thing_search_alias_endpoint_is_available(authenticated_headers):
    with TestClient(app) as client:
        response = client.get(
            "/api/things/search",
            headers=authenticated_headers,
            params={"q": "line temperature monitor", "k": 3},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "id": "urn:thing:search-stub",
                "title": "Stub result for line temperature monitor",
                "description": "Stubbed semantic search result",
                "tags": ["stub"],
                "score": 1.0,
                "summary": "Matched with k=3",
            }
        ],
        "query": "line temperature monitor",
    }


def test_legacy_search_endpoint_is_removed(authenticated_headers):
    with TestClient(app) as client:
        response = client.get(
            "/api/search",
            headers=authenticated_headers,
            params={"q": "line temperature monitor", "k": 3},
        )

    assert response.status_code == 404, response.text


def test_index_status_endpoint_returns_semantic_index_details(authenticated_headers):
    thing = sample_thing()

    with TestClient(app) as client:
        create_response = client.post(
            "/api/things",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=thing,
        )
        assert create_response.status_code == 201, create_response.text

        response = client.get(
            f"/api/index-status/{thing['id']}",
            headers=authenticated_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "thing_id": thing["id"],
        "indexed": True,
        "stale": False,
        "indexed_at": "2026-03-16T00:00:00+00:00",
        "summary_source": "stub",
        "summary_model": "stub-model",
        "prompt_version": "v-test",
        "td_hash_match": True,
        "summary": "Stubbed semantic summary",
        "location_candidates": ["Line 3"],
        "property_names": ["temperature"],
        "action_names": ["toggle"],
        "event_names": ["overheated"],
    }


def test_thing_index_status_alias_endpoint_returns_semantic_index_details(
    authenticated_headers,
):
    thing = sample_thing("urn:thing:search-alias")

    with TestClient(app) as client:
        create_response = client.post(
            "/api/things",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json=thing,
        )
        assert create_response.status_code == 201, create_response.text

        response = client.get(
            f"/api/things/{thing['id']}/index-status",
            headers=authenticated_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "thing_id": thing["id"],
        "indexed": True,
        "stale": False,
        "indexed_at": "2026-03-16T00:00:00+00:00",
        "summary_source": "stub",
        "summary_model": "stub-model",
        "prompt_version": "v-test",
        "td_hash_match": True,
        "summary": "Stubbed semantic summary",
        "location_candidates": ["Line 3"],
        "property_names": ["temperature"],
        "action_names": ["toggle"],
        "event_names": ["overheated"],
    }
