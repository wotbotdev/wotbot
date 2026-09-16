from __future__ import annotations

from typing import Any, ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wotbot.auth import User
from wotbot.auth.providers import get_current_user
from wotbot.virtual_things.routes import router
from wotbot.virtual_things.schemas import (
    DefineVirtualThingRequest,
    VirtualThingDefinition,
)
from wotbot.virtual_things.store import VirtualThingStateConflict

THING_ID = "virtual:things:comfort-sensor"


def _definition(
    *,
    status: str = "active",
    version: int = 1,
    shared_state: dict[str, Any] | None = None,
) -> VirtualThingDefinition:
    return VirtualThingDefinition(
        id=THING_ID,
        title="Comfort Sensor",
        description="Virtual comfort signal",
        td={
            "id": THING_ID,
            "title": "Comfort Sensor",
            "properties": {"temperature": {"type": "number"}},
        },
        version=version,
        status=status,  # type: ignore[arg-type]
        shared_state=dict(shared_state or {}),
        bindings=[
            {
                "affordance_type": "property",
                "affordance_name": "temperature",
                "kind": "computed",
                "handler_code": "def handle(input, state, context):\n    return 21",
            }
        ],
    )


class _FakeVirtualThingStore:
    definitions: ClassVar[dict[str, VirtualThingDefinition]] = {}

    def list_definitions(
        self,
        *,
        include_disabled: bool = False,
    ) -> list[VirtualThingDefinition]:
        return [
            definition
            for definition in self.definitions.values()
            if include_disabled or definition.status == "active"
        ]

    def get_definition(
        self,
        thing_id: str,
        *,
        include_disabled: bool = False,
    ) -> VirtualThingDefinition:
        definition = self.definitions[thing_id]
        if definition.status != "active" and not include_disabled:
            raise KeyError(thing_id)
        return definition

    def define_thing(self, request: DefineVirtualThingRequest) -> VirtualThingDefinition:
        previous = self.definitions.get(request.id or "", _definition())
        definition = VirtualThingDefinition(
            id=request.id or THING_ID,
            title=request.title,
            description=request.description,
            owner_thread_id=request.owner_thread_id,
            td=request.td,
            version=previous.version + 1,
            status=request.status,
            shared_state=(
                request.shared_state if request.shared_state is not None else previous.shared_state
            ),
            bindings=request.bindings,
        )
        self.definitions[definition.id] = definition
        return definition

    def delete_thing(self, thing_id: str) -> None:
        del self.definitions[thing_id]


class _FakeValidator:
    requests: ClassVar[list[DefineVirtualThingRequest]] = []

    async def validate(
        self,
        request: DefineVirtualThingRequest,
        *,
        run_smoke: bool,
    ) -> dict[str, Any]:
        self.requests.append(request)
        return {"ok": True, "smoke_tested": run_smoke, "issues": []}


def _client_for_user(user: User, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(
        "wotbot.virtual_things.routes.VirtualThingStore",
        _FakeVirtualThingStore,
    )
    monkeypatch.setattr(
        "wotbot.virtual_things.routes.VirtualThingValidator",
        _FakeValidator,
    )
    _FakeVirtualThingStore.definitions = {THING_ID: _definition()}
    _FakeValidator.requests = []
    return TestClient(app)


def _define_payload() -> dict[str, Any]:
    return {
        "id": THING_ID,
        "title": "Comfort Sensor",
        "description": "Updated comfort signal",
        "td": {
            "id": THING_ID,
            "title": "Comfort Sensor",
            "properties": {"temperature": {"type": "number"}},
        },
        "status": "active",
        "bindings": [
            {
                "affordance_type": "property",
                "affordance_name": "temperature",
                "kind": "computed",
                "handler_code": "def handle(input, state, context):\n    return 22",
            }
        ],
    }


def test_virtual_things_routes_accept_api_key_user_with_thing_scopes(monkeypatch):
    user = User(
        user_id="admin-key",
        scopes=["things:read", "things:write", "things:delete"],
        auth_type="api_key",
    )

    with _client_for_user(user, monkeypatch) as client:
        list_response = client.get("/api/virtual-things/definitions")
        assert list_response.status_code == 200
        assert list_response.json()["definitions"][0]["id"] == THING_ID

        get_response = client.get(f"/api/virtual-things/definitions/{THING_ID}")
        assert get_response.status_code == 200
        assert get_response.json()["bindings"][0]["handler_code"].endswith("return 21")

        put_response = client.put(
            f"/api/virtual-things/definitions/{THING_ID}",
            json=_define_payload(),
        )
        assert put_response.status_code == 200
        assert put_response.json()["description"] == "Updated comfort signal"
        assert put_response.json()["bindings"][0]["handler_code"].endswith("return 22")

        delete_response = client.delete(f"/api/virtual-things/definitions/{THING_ID}")
        assert delete_response.status_code == 200
        assert delete_response.json() == {"ok": True, "thing_id": THING_ID}


def test_servient_definition_view_omits_handler_internals(monkeypatch):
    user = User(
        user_id="virtual-servient",
        scopes=["things:read"],
        auth_type="api_key",
    )

    with _client_for_user(user, monkeypatch) as client:
        list_response = client.get("/api/virtual-things/servient/definitions")
        assert list_response.status_code == 200
        listed_binding = list_response.json()["definitions"][0]["bindings"][0]
        assert set(listed_binding) == {
            "affordance_type",
            "affordance_name",
            "kind",
            "trigger",
        }

        get_response = client.get(f"/api/virtual-things/servient/definitions/{THING_ID}")
        assert get_response.status_code == 200
        servient_binding = get_response.json()["bindings"][0]
        for leaked in ("handler_code", "capabilities", "config", "state"):
            assert leaked not in servient_binding

        # The full endpoint (used by the UI) still carries handler internals.
        full_response = client.get(f"/api/virtual-things/definitions/{THING_ID}")
        assert "handler_code" in full_response.json()["bindings"][0]


def test_virtual_things_routes_reject_api_key_user_missing_scope(monkeypatch):
    user = User(
        user_id="writer-key",
        scopes=["things:write"],
        auth_type="api_key",
    )

    with _client_for_user(user, monkeypatch) as client:
        response = client.get("/api/virtual-things/definitions")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing required scopes: things:read"


def test_virtual_things_route_uses_existing_shared_state_for_validation(monkeypatch):
    user = User(
        user_id="admin-key",
        scopes=["things:write"],
        auth_type="api_key",
    )

    with _client_for_user(user, monkeypatch) as client:
        _FakeVirtualThingStore.definitions = {
            THING_ID: _definition(status="disabled", shared_state={"power": False})
        }
        payload = _define_payload()
        payload["status"] = "active"
        payload["bindings"][0]["affordance_name"] = "power"
        payload["bindings"][0]["handler_code"] = (
            "def handle(input, state, context):\n    return context['shared_state']['power']"
        )
        payload["td"]["properties"] = {"power": {"type": "boolean"}}

        response = client.put(
            f"/api/virtual-things/definitions/{THING_ID}",
            json=payload,
        )

    assert response.status_code == 200
    assert _FakeValidator.requests[-1].shared_state == {"power": False}


def test_virtual_thing_state_conflict_maps_to_409():
    from wotbot.virtual_things.routes import virtual_thing_http_error

    error = virtual_thing_http_error(VirtualThingStateConflict("state changed"))

    assert error.status_code == 409
    assert error.detail == "state changed"
