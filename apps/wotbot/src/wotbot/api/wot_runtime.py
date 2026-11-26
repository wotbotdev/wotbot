"""Browser-facing proxy for the wot-runtime service.

The agent reaches the runtime via :class:`WotRuntimeClient` directly, but
generated mini-interfaces (see ``create_web_interface``) need a browser-reachable
path. These endpoints wrap the same client so the privileged
``WOT_RUNTIME_API_TOKEN`` never leaves the backend, and add an SSE endpoint that
relays observe/subscribe deliveries from the ``wot_runtime_events`` stream.

Authorization reuses the registry user scopes: reads/observes require
``things:read``; writes/invocations require ``things:write``. Capability scoping
per generated interface is enforced in the trusted parent UI before it ever
calls these routes.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from wotbot.auth import User, require_scopes
from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.settings import Settings
from wotbot.jobs.stream import parse_runtime_stream_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wot/runtime", tags=["wot-runtime"])

# Stream event types worth relaying to a live interface.
_RELAYED_EVENT_TYPES = {"property_observed", "event_received"}
_SSE_BLOCK_MS = 15000


def _runtime_client(request: Request) -> WotRuntimeClient:
    settings = getattr(request.app.state, "settings", None) or Settings()
    return WotRuntimeClient(settings)


class ReadPropertyBody(BaseModel):
    thing_id: str
    property_name: str
    uri_variables: dict[str, Any] | None = None
    form_index: int | None = None


class WritePropertyBody(BaseModel):
    thing_id: str
    property_name: str
    value: Any = None
    value_content_type: str | None = None
    value_base64: str | None = None
    uri_variables: dict[str, Any] | None = None
    form_index: int | None = None


class InvokeActionBody(BaseModel):
    thing_id: str
    action_name: str
    input: Any = None
    input_content_type: str | None = None
    input_base64: str | None = None
    uri_variables: dict[str, Any] | None = None
    form_index: int | None = None
    idempotency_key: str | None = None


class ObservePropertyBody(BaseModel):
    thing_id: str
    property_name: str
    uri_variables: dict[str, Any] | None = None
    form_index: int | None = None


class SubscribeEventBody(BaseModel):
    thing_id: str
    event_name: str
    subscription_input: Any = None
    subscription_input_content_type: str | None = None
    subscription_input_base64: str | None = None
    uri_variables: dict[str, Any] | None = None
    form_index: int | None = None


class RemoveSubscriptionBody(BaseModel):
    subscription_id: str
    cancellation_input: Any = None
    cancellation_input_content_type: str | None = None
    cancellation_input_base64: str | None = None


async def _call(coro) -> dict[str, Any]:
    try:
        return await coro
    except LookupError as exc:  # Thing not visible in the caller's workspace
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - transport/connection failures
        logger.warning("wot-runtime proxy call failed: %s", exc)
        raise HTTPException(status_code=503, detail="wot-runtime is unavailable") from exc


@router.post("/read-property")
async def read_property(
    body: ReadPropertyBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return await _call(
        _runtime_client(request).read_property(
            thing_id=body.thing_id,
            property_name=body.property_name,
            uri_variables=body.uri_variables,
            form_index=body.form_index,
        )
    )


@router.post("/write-property")
async def write_property(
    body: WritePropertyBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    return await _call(
        _runtime_client(request).write_property(
            thing_id=body.thing_id,
            property_name=body.property_name,
            value=body.value,
            value_content_type=body.value_content_type,
            value_base64=body.value_base64,
            uri_variables=body.uri_variables,
            form_index=body.form_index,
        )
    )


@router.post("/invoke-action")
async def invoke_action(
    body: InvokeActionBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    result = await _call(
        _runtime_client(request).invoke_action(
            thing_id=body.thing_id,
            action_name=body.action_name,
            input=body.input,
            input_content_type=body.input_content_type,
            input_base64=body.input_base64,
            uri_variables=body.uri_variables,
            form_index=body.form_index,
            idempotency_key=body.idempotency_key,
        )
    )
    challenge = _credential_challenge(result)
    if challenge is not None:
        raise HTTPException(status_code=428, detail=challenge)
    return result


def _credential_challenge(result: dict[str, Any]) -> dict[str, str] | None:
    candidate = result.get("result") or result.get("completed_result")
    if not isinstance(candidate, dict):
        return None
    payload = candidate.get("payload")
    value = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(value, dict) or value.get("status") not in {
        "credential_required",
        "credential_rejected",
    }:
        return None
    safe: dict[str, str] = {}
    for key in (
        "status",
        "owner_kind",
        "thing_id",
        "source_id",
        "security_name",
        "scheme",
        "message",
    ):
        if isinstance(value.get(key), str):
            safe[key] = value[key]
    required = {"status", "security_name", "scheme"}
    has_owner = "thing_id" in safe or (safe.get("owner_kind") == "source" and "source_id" in safe)
    return safe if required.issubset(safe) and has_owner else None


@router.post("/observe-property")
async def observe_property(
    body: ObservePropertyBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return await _call(
        _runtime_client(request).observe_property(
            thing_id=body.thing_id,
            property_name=body.property_name,
            uri_variables=body.uri_variables,
            form_index=body.form_index,
        )
    )


@router.post("/subscribe-event")
async def subscribe_event(
    body: SubscribeEventBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return await _call(
        _runtime_client(request).subscribe_event(
            thing_id=body.thing_id,
            event_name=body.event_name,
            subscription_input=body.subscription_input,
            subscription_input_content_type=body.subscription_input_content_type,
            subscription_input_base64=body.subscription_input_base64,
            uri_variables=body.uri_variables,
            form_index=body.form_index,
        )
    )


@router.post("/remove-subscription")
async def remove_subscription(
    body: RemoveSubscriptionBody,
    request: Request,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return await _call(
        _runtime_client(request).remove_subscription(
            subscription_id=body.subscription_id,
            cancellation_input=body.cancellation_input,
            cancellation_input_content_type=body.cancellation_input_content_type,
            cancellation_input_base64=body.cancellation_input_base64,
        )
    )


def _binary_payload(payload_base64: str, content_type: str, size_bytes: int) -> dict[str, Any]:
    return {
        "kind": "binary",
        "contentType": content_type or "application/octet-stream",
        "bodyBase64": payload_base64,
        "sizeBytes": size_bytes,
    }


def _is_binary_content_type(content_type: str) -> bool:
    normalized = content_type.lower()
    return bool(content_type) and "json" not in normalized and not normalized.startswith("text/")


def _decode_payload(payload_base64: str, content_type: str) -> Any:
    """Best-effort decode of a stream payload into a JSON-friendly value."""
    normalized_content_type = (content_type or "").lower()
    if not payload_base64:
        if _is_binary_content_type(content_type):
            return _binary_payload("", content_type, 0)
        return None
    try:
        raw = base64.b64decode(payload_base64)
    except (binascii.Error, ValueError):
        return None
    if not normalized_content_type or "json" in normalized_content_type:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _binary_payload(payload_base64, content_type, len(raw))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if normalized_content_type.startswith("text/"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return _binary_payload(payload_base64, content_type, len(raw))
    return _binary_payload(payload_base64, content_type, len(raw))


async def _event_stream(
    request: Request,
    *,
    redis_url: str,
    stream: str,
    subscription_ids: set[str],
):
    client = redis.from_url(redis_url, decode_responses=True)
    last_id = "$"
    try:
        # Prime the SSE so the browser's EventSource opens promptly.
        yield ": connected\n\n"
        while not await request.is_disconnected():
            records = await client.xread({stream: last_id}, block=_SSE_BLOCK_MS, count=20)
            if not records:
                continue
            for _stream_name, entries in records:
                for entry_id, fields in entries:
                    last_id = entry_id
                    event = parse_runtime_stream_fields(fields)
                    if event["event_type"] not in _RELAYED_EVENT_TYPES:
                        continue
                    if event["subscription_id"] not in subscription_ids:
                        continue
                    message = {
                        "eventType": event["event_type"],
                        "subscriptionId": event["subscription_id"],
                        "thingId": event["thing_id"],
                        "name": event["name"],
                        "timestamp": event["timestamp"],
                        "value": _decode_payload(event["payload_base64"], event["content_type"]),
                    }
                    yield f"data: {json.dumps(message)}\n\n"
    finally:
        await client.aclose()


@router.get("/events")
async def events(
    request: Request,
    subscriptions: str = Query(..., description="Comma-separated subscription ids."),
    _user: User = Depends(require_scopes(["things:read"])),
) -> StreamingResponse:
    subscription_ids = {s for s in (subscriptions or "").split(",") if s}
    if not subscription_ids:
        raise HTTPException(status_code=400, detail="No subscription ids provided")

    settings = getattr(request.app.state, "settings", None) or Settings()
    generator = _event_stream(
        request,
        redis_url=settings.redis_url,
        stream=settings.wot_runtime_stream,
        subscription_ids=subscription_ids,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
