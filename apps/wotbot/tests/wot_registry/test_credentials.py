from fastapi.testclient import TestClient

from wotbot.api.main import app
from wotbot.core.config import get_settings


def _wot_runtime_headers(token: str) -> dict[str, str]:
    return {
        "X-Registry-Service": "wot_runtime",
        "X-Registry-Service-Token": token,
    }


def test_runtime_secrets_endpoint_requires_wot_runtime_service(
    authenticated_headers,
    monkeypatch,
):
    monkeypatch.setenv("WOT_RUNTIME_REGISTRY_TOKEN", "runtime-secret")
    get_settings.cache_clear()

    with TestClient(app) as client:
        upsert_response = client.put(
            "/api/credentials/urn%3Athing%3Aalpha/basic_sc",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json={
                "scheme": "basic",
                "credentials": {
                    "username": "demo-user",
                    "password": "demo-pass",
                },
            },
        )
        assert upsert_response.status_code == 200, upsert_response.text

        second_upsert_response = client.put(
            "/api/credentials/urn%3Athing%3Aalpha/token_sc",
            headers={
                **authenticated_headers,
                "Content-Type": "application/json",
            },
            json={
                "scheme": "bearer",
                "credentials": {
                    "token": "demo-token",
                },
            },
        )
        assert second_upsert_response.status_code == 200, second_upsert_response.text

        metadata_response = client.get(
            "/api/credentials/urn%3Athing%3Aalpha",
            headers=authenticated_headers,
        )
        assert metadata_response.status_code == 200, metadata_response.text
        metadata_items = metadata_response.json()["items"]
        assert [item["security_name"] for item in metadata_items] == [
            "basic_sc",
            "token_sc",
        ]
        assert all(item["has_credentials"] for item in metadata_items)
        assert all("credentials" not in item for item in metadata_items)

        forbidden_response = client.get(
            "/api/runtime/secrets",
            headers=authenticated_headers,
        )
        assert forbidden_response.status_code == 403
        assert forbidden_response.json()["detail"] == "Service authentication required"

        allowed_response = client.get(
            "/api/runtime/secrets",
            headers=_wot_runtime_headers("runtime-secret"),
        )
        assert allowed_response.status_code == 200, allowed_response.text
        assert allowed_response.json() == {
            "urn:thing:alpha": {
                "entries": [
                    {
                        "security_name": "basic_sc",
                        "scheme": "basic",
                        "credentials": {
                            "username": "demo-user",
                            "password": "demo-pass",
                        },
                    },
                    {
                        "security_name": "token_sc",
                        "scheme": "bearer",
                        "credentials": {
                            "token": "demo-token",
                        },
                    },
                ]
            }
        }
