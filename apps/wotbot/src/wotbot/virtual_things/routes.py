from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from wotbot.auth import User, require_scopes
from wotbot.catalog.ids import decode_thing_id
from wotbot.core.time import utc_now
from wotbot.jobs.records.http import virtual_record_http_error
from wotbot.jobs.records.ids import is_virtual_record_thing_id
from wotbot.virtual_things.dispatcher import VirtualThingDispatcher
from wotbot.virtual_things.handler import VirtualThingHandlerError
from wotbot.virtual_things.schemas import (
    DefineVirtualThingRequest,
    VirtualThingServientView,
)
from wotbot.virtual_things.store import VirtualThingStateConflict, VirtualThingStore
from wotbot.virtual_things.validator import VirtualThingValidator

router = APIRouter(tags=["virtual-things"])


def get_virtual_thing_store() -> VirtualThingStore:
    return VirtualThingStore()


def get_virtual_thing_dispatcher() -> VirtualThingDispatcher:
    return VirtualThingDispatcher()


def get_virtual_thing_validator() -> VirtualThingValidator:
    return VirtualThingValidator()


@router.get("/api/virtual-things/definitions")
def list_virtual_thing_definitions(
    include_disabled: bool = False,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    definitions = store.list_definitions(include_disabled=include_disabled)
    return {
        "definitions": [
            definition.model_dump(mode="json", by_alias=True) for definition in definitions
        ]
    }


@router.get("/api/virtual-things/servient/definitions")
def list_servient_definitions(
    include_disabled: bool = False,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    """Narrow definition view consumed by virtual-servient.

    Unlike the full ``/definitions`` endpoint (used by the UI for editing), this
    omits handler code, capability grants, config, and state so the servient
    never receives data it cannot use.
    """
    definitions = store.list_definitions(include_disabled=include_disabled)
    return {
        "definitions": [
            VirtualThingServientView.from_definition(definition).model_dump(mode="json")
            for definition in definitions
        ]
    }


@router.get("/api/virtual-things/servient/definitions/{thing_id:path}")
def get_servient_definition(
    thing_id: str,
    include_disabled: bool = False,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        definition = store.get_definition(decoded_thing_id, include_disabled=include_disabled)
        return VirtualThingServientView.from_definition(definition).model_dump(mode="json")
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.get("/api/virtual-things/definitions/{thing_id:path}")
def get_virtual_thing_definition(
    thing_id: str,
    include_disabled: bool = False,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        definition = store.get_definition(
            decoded_thing_id,
            include_disabled=include_disabled,
        )
        return definition.model_dump(mode="json", by_alias=True)
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.get("/api/virtual-things/{thing_id:path}/properties/{property_name}")
async def read_virtual_thing_property(
    thing_id: str,
    property_name: str,
    dispatcher: VirtualThingDispatcher = Depends(get_virtual_thing_dispatcher),
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        return {
            "thing_id": decoded_thing_id,
            "property_name": property_name,
            "value": await dispatcher.read_property(
                decoded_thing_id,
                property_name,
            ),
        }
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.post("/api/virtual-things/{thing_id:path}/actions/{action_name}")
async def invoke_virtual_thing_action(
    thing_id: str,
    action_name: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    dispatcher: VirtualThingDispatcher = Depends(get_virtual_thing_dispatcher),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        return {
            "thing_id": decoded_thing_id,
            "action_name": action_name,
            "value": await dispatcher.invoke_action(
                decoded_thing_id,
                action_name,
                payload.get("input"),
            ),
        }
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.post("/api/virtual-things/{thing_id:path}/events/{event_name}/evaluate")
async def evaluate_virtual_thing_event(
    thing_id: str,
    event_name: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    dispatcher: VirtualThingDispatcher = Depends(get_virtual_thing_dispatcher),
    _user: User = Depends(require_scopes(["things:write"])),
) -> Any:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        return await dispatcher.evaluate_event(
            decoded_thing_id,
            event_name,
            payload.get("input"),
            dry_run=bool(payload.get("dry_run")),
        )
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.post("/api/virtual-things/{thing_id:path}/events/{event_name}/emit")
async def emit_virtual_thing_event(
    thing_id: str,
    event_name: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    dispatcher: VirtualThingDispatcher = Depends(get_virtual_thing_dispatcher),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        result = await dispatcher.emit_event(
            decoded_thing_id,
            event_name,
            {
                "trigger": "explicit",
                "input": payload.get("input"),
                "requested_at": utc_now().isoformat(),
            },
        )
        return result
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


@router.put("/api/virtual-things/definitions/{thing_id:path}")
async def define_virtual_thing_definition(
    thing_id: str,
    payload: DefineVirtualThingRequest,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    validator: VirtualThingValidator = Depends(get_virtual_thing_validator),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        if payload.id and payload.id != decoded_thing_id:
            raise ValueError("Thing id in path and body must match")
        request_data = payload.model_dump(mode="json")
        request_data["id"] = decoded_thing_id
        request_data["td"] = {**payload.td, "id": decoded_thing_id}
        if request_data.get("shared_state") is None:
            try:
                existing = store.get_definition(decoded_thing_id, include_disabled=True)
                request_data["shared_state"] = existing.shared_state
            except KeyError:
                pass
        request = DefineVirtualThingRequest.model_validate(request_data)
        validation_report = await validator.validate(
            request,
            run_smoke=request.status == "active",
        )
        if not validation_report["ok"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "virtual thing validation failed",
                    "validation_report": validation_report,
                },
            )
        definition = await asyncio.to_thread(store.define_thing, request)
        return definition.model_dump(mode="json", by_alias=True)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise virtual_thing_http_error(exc) from exc


@router.delete("/api/virtual-things/definitions/{thing_id:path}")
def delete_virtual_thing_definition(
    thing_id: str,
    store: VirtualThingStore = Depends(get_virtual_thing_store),
    _user: User = Depends(require_scopes(["things:delete"])),
) -> dict[str, Any]:
    try:
        decoded_thing_id = decode_thing_id(thing_id)
        if is_virtual_record_thing_id(decoded_thing_id):
            raise ValueError("virtual record Things are deleted with their owning job")
        store.delete_thing(decoded_thing_id)
        return {"ok": True, "thing_id": decoded_thing_id}
    except Exception as exc:
        raise virtual_thing_http_error(exc) from exc


def virtual_thing_http_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, VirtualThingStateConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(error, TimeoutError):
        return HTTPException(status_code=504, detail=str(error))
    if isinstance(error, VirtualThingHandlerError):
        return HTTPException(status_code=502, detail=f"Virtual Thing handler failed: {error}")
    return virtual_record_http_error(error)
