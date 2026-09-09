"""Shared capability enforcement and WoT execution for generated MCP Apps."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from wotbot.a2a.artifacts import ArtifactStore
from wotbot.a2a.constants import MCP_APP_MIME_TYPE
from wotbot.auth.models import User
from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.config import get_settings
from wotbot.core.database import get_session_factory
from wotbot.panels.models import PanelVersion
from wotbot.jobs.stream import parse_runtime_stream_fields
from wotbot.runtime_events import decode_runtime_payload

RELAYED_RUNTIME_EVENT_TYPES = {"property_observed", "event_received"}
_CURSOR = re.compile(r"(\d+-\d+|0)")
SUBSCRIPTION_TOOLS = {"things.observe_property", "things.subscribe_event"}

# Every open panel long-polls for its own events, so these calls arrive
# continuously. Connecting per call made each poll pay a TCP and handshake
# round trip; one pooled client per URL does not.
_clients: dict[str, redis.Redis] = {}


def runtime_stream_client(redis_url: str) -> redis.Redis:
    client = _clients.get(redis_url)
    if client is None:
        client = redis.from_url(redis_url, decode_responses=True)
        _clients[redis_url] = client
    return client


async def close_runtime_stream_clients() -> None:
    """Release pooled clients at shutdown."""
    while _clients:
        _, client = _clients.popitem()
        await client.aclose()


@dataclass(frozen=True, slots=True)
class PinnedPanel:
    html: str
    title: str
    capabilities: list[Any]


async def load_pinned_version(panel_version_id: str | None) -> PinnedPanel | None:
    """Read the immutable panel version an MCP App is bound to.

    Markup and allowlist are not copied into the artifact row: the version is
    already immutable, so reading it live keeps one source of truth and lets
    the document be re-wrapped with the current bridge and CSP.
    """
    if not panel_version_id:
        return None
    return await asyncio.to_thread(_load_pinned_version, panel_version_id)


def _load_pinned_version(panel_version_id: str) -> PinnedPanel | None:
    with get_session_factory()() as session:
        row = session.get(PanelVersion, panel_version_id)
        if row is None:
            return None
        return PinnedPanel(
            html=row.html, title=row.title, capabilities=list(row.capabilities or [])
        )


class PanelActionService:
    def __init__(self, artifact_store: ArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store or ArtifactStore()

    async def execute(
        self,
        *,
        artifact_id: str,
        owner: str,
        user: User,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        artifact = await self.artifact_store.get(artifact_id, owner=owner)
        if (
            artifact is None
            or artifact.media_type != MCP_APP_MIME_TYPE
            or (artifact.artifact_metadata or {}).get("bridgeVersion") != 2
        ):
            raise PermissionError("Unknown MCP App artifact")

        metadata = artifact.artifact_metadata or {}
        pinned = await load_pinned_version(metadata.get("panelVersionId"))
        if pinned is None:
            raise PermissionError("Unknown MCP App artifact")
        authorize_app_tool(
            tool_name,
            arguments,
            pinned.capabilities,
            metadata.get("subscriptions") or [],
        )
        require_tool_scope(user, tool_name)

        settings = get_settings()
        starting_cursor = None
        if tool_name == "things.next_subscription_event":
            # The panel carries its own stream position, so an open
            # subscription polls without writing anything.
            subscription_id = required_subscription_id(arguments)
            cursor = requested_cursor(arguments)
            if cursor is None:
                # A panel that lost its position starts from now rather than
                # replaying the whole runtime stream.
                cursor = await runtime_stream_tail(
                    redis_url=settings.redis_url, stream=settings.wot_runtime_stream
                )
            result, next_cursor = await next_subscription_event(
                redis_url=settings.redis_url,
                stream=settings.wot_runtime_stream,
                subscription_id=subscription_id,
                cursor=cursor,
                timeout_ms=subscription_poll_timeout_ms(settings, arguments),
            )
            result = {**result, "nextCursor": next_cursor}
        else:
            if tool_name in SUBSCRIPTION_TOOLS:
                starting_cursor = await runtime_stream_tail(
                    redis_url=settings.redis_url,
                    stream=settings.wot_runtime_stream,
                )
            result = await call_runtime_tool(WotRuntimeClient(settings), tool_name, arguments)

        returned_subscription_id = subscription_id_from_result(result)
        if returned_subscription_id:
            await self.artifact_store.remember_subscription(
                artifact_id, owner=owner, subscription_id=returned_subscription_id
            )
            if starting_cursor is not None and isinstance(result, dict):
                # Hand back the position captured before subscribing, so the
                # first poll cannot miss an event delivered in between.
                result = {**result, "cursor": starting_cursor}
        if tool_name == "things.unsubscribe":
            await self.artifact_store.forget_subscription(
                artifact_id, owner=owner, subscription_id=required_subscription_id(arguments)
            )
        return result


def authorize_app_tool(
    tool_name: str,
    arguments: dict[str, Any],
    capabilities: list[Any],
    subscriptions: list[Any],
) -> None:
    if tool_name in {"things.next_subscription_event", "things.unsubscribe"}:
        subscription_id = arguments.get("subscriptionId") or arguments.get("subscription_id")
        if not isinstance(subscription_id, str) or subscription_id not in subscription_ids(
            subscriptions
        ):
            raise PermissionError("Subscription is outside this MCP App's capability set")
        return

    thing_id = arguments.get("thingId") or arguments.get("thing_id")
    affordance_key = {
        "things.read_property": "propertyName",
        "things.write_property": "propertyName",
        "things.observe_property": "propertyName",
        "things.invoke_action": "actionName",
        "things.subscribe_event": "eventName",
    }.get(tool_name)
    affordance = arguments.get(affordance_key) if affordance_key else None
    if not isinstance(thing_id, str) or not isinstance(affordance, str):
        raise ValueError("Thing tool arguments are incomplete")
    for capability in capabilities:
        if not isinstance(capability, dict):
            continue
        if capability.get("thingId") != thing_id:
            continue
        operation = {
            "things.read_property": "readProperty",
            "things.write_property": "writeProperty",
            "things.invoke_action": "invokeAction",
            "things.observe_property": "observeProperty",
            "things.subscribe_event": "subscribeEvent",
        }.get(tool_name)
        if operation not in (capability.get("ops") or []):
            continue
        if affordance in (capability.get("affordances") or []):
            return
    raise PermissionError("Tool call is outside this MCP App's capability set")


def require_tool_scope(user: User, tool_name: str) -> None:
    required = "agent:invoke"
    if required not in set(user.scopes or []):
        raise PermissionError(f"Missing required scope: {required}")


def _arg(arguments: dict[str, Any], camel: str, snake: str, default: Any = None) -> Any:
    return arguments.get(camel, arguments.get(snake, default))


async def call_runtime_tool(
    client: WotRuntimeClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    if tool_name == "things.read_property":
        result = await client.read_property(
            thing_id=_arg(arguments, "thingId", "thing_id"),
            property_name=_arg(arguments, "propertyName", "property_name"),
            uri_variables=_arg(arguments, "uriVariables", "uri_variables"),
            form_index=_arg(arguments, "formIndex", "form_index"),
        )
        return decoded_runtime_value(result)
    if tool_name == "things.write_property":
        result = await client.write_property(
            thing_id=_arg(arguments, "thingId", "thing_id"),
            property_name=_arg(arguments, "propertyName", "property_name"),
            value=arguments.get("value"),
            value_content_type=_arg(arguments, "valueContentType", "value_content_type"),
            value_base64=_arg(arguments, "valueBase64", "value_base64"),
            uri_variables=_arg(arguments, "uriVariables", "uri_variables"),
            form_index=_arg(arguments, "formIndex", "form_index"),
        )
        return decoded_runtime_value(result)
    if tool_name == "things.invoke_action":
        result = await client.invoke_action(
            thing_id=_arg(arguments, "thingId", "thing_id"),
            action_name=_arg(arguments, "actionName", "action_name"),
            input=arguments.get("input"),
            input_content_type=_arg(arguments, "inputContentType", "input_content_type"),
            input_base64=_arg(arguments, "inputBase64", "input_base64"),
            uri_variables=_arg(arguments, "uriVariables", "uri_variables"),
            form_index=_arg(arguments, "formIndex", "form_index"),
            idempotency_key=_arg(arguments, "idempotencyKey", "idempotency_key"),
        )
        return decoded_runtime_value(result)
    if tool_name == "things.observe_property":
        return await client.observe_property(
            thing_id=_arg(arguments, "thingId", "thing_id"),
            property_name=_arg(arguments, "propertyName", "property_name"),
            uri_variables=_arg(arguments, "uriVariables", "uri_variables"),
            form_index=_arg(arguments, "formIndex", "form_index"),
        )
    if tool_name == "things.subscribe_event":
        return await client.subscribe_event(
            thing_id=_arg(arguments, "thingId", "thing_id"),
            event_name=_arg(arguments, "eventName", "event_name"),
            subscription_input=arguments.get("input", arguments.get("subscription_input")),
            subscription_input_content_type=_arg(
                arguments, "inputContentType", "subscription_input_content_type"
            ),
            subscription_input_base64=_arg(arguments, "inputBase64", "subscription_input_base64"),
            uri_variables=_arg(arguments, "uriVariables", "uri_variables"),
            form_index=_arg(arguments, "formIndex", "form_index"),
        )
    if tool_name == "things.unsubscribe":
        return await client.remove_subscription(
            subscription_id=_arg(arguments, "subscriptionId", "subscription_id"),
            cancellation_input=_arg(arguments, "cancellationInput", "cancellation_input"),
            cancellation_input_content_type=_arg(
                arguments,
                "cancellationInputContentType",
                "cancellation_input_content_type",
            ),
            cancellation_input_base64=_arg(
                arguments,
                "cancellationInputBase64",
                "cancellation_input_base64",
            ),
        )
    raise ValueError(f"Unsupported MCP App tool: {tool_name}")


def decoded_runtime_value(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    candidate = result.get("result") or result.get("completed_result")
    if isinstance(candidate, dict):
        if candidate.get("success") is False:
            return {"error": candidate.get("status_text") or "Interaction failed"}
        payload = candidate.get("payload")
        if isinstance(payload, dict):
            value = payload.get("data", payload)
            if isinstance(value, dict) and value.get("status") in {
                "credential_required",
                "credential_rejected",
            }:
                return {
                    "error": "This operation needs valid credentials. Provision them through WoTBot's credential settings or API, then retry the panel operation."
                }
            return value
        return None
    if result.get("outcome") == "operation_handle":
        return result.get("operation_handle")
    return result


def subscription_id_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("subscription_id", "subscriptionId"):
        if isinstance(result.get(key), str):
            return result[key]
    for key in ("subscription", "result", "completed_result", "payload", "data"):
        nested = subscription_id_from_result(result.get(key))
        if nested:
            return nested
    return None


def subscription_ids(subscriptions: list[Any]) -> set[str]:
    return {value for value in subscriptions if isinstance(value, str)}


def required_subscription_id(arguments: dict[str, Any]) -> str:
    subscription_id = _arg(arguments, "subscriptionId", "subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise ValueError("subscriptionId is required")
    return subscription_id


def requested_cursor(arguments: dict[str, Any]) -> str | None:
    """Validate the stream position the panel wants to resume from.

    Replaying from an older position only re-delivers events this panel was
    already entitled to, so the value needs shape checking, not authorization.
    ``None`` means the caller has no position and should start from the tail.
    """
    cursor = _arg(arguments, "cursor", "cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
        raise ValueError("cursor must be a Redis stream ID")
    return cursor


def subscription_poll_timeout_ms(settings: Any, arguments: dict[str, Any]) -> int:
    default_ms = int(float(settings.wot_runtime_subscription_timeout_seconds) * 1000)
    requested = _arg(arguments, "timeoutMs", "timeout_ms", default_ms)
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        raise ValueError("timeoutMs must be a number")
    return max(1, min(int(requested), 30_000))


async def runtime_stream_tail(*, redis_url: str, stream: str) -> str:
    client = runtime_stream_client(redis_url)
    entries = await client.xrevrange(stream, max="+", min="-", count=1)
    return entries[0][0] if entries else "0-0"


async def next_subscription_event(
    *,
    redis_url: str,
    stream: str,
    subscription_id: str,
    cursor: str,
    timeout_ms: int,
) -> tuple[dict[str, Any], str]:
    client = runtime_stream_client(redis_url)
    current_cursor = cursor
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return {"event": None}, current_cursor
        records = await client.xread(
            {stream: current_cursor},
            block=max(1, remaining_ms),
            count=100,
        )
        if not records:
            return {"event": None}, current_cursor
        for _stream_name, entries in records:
            for entry_id, fields in entries:
                current_cursor = entry_id
                event = parse_runtime_stream_fields(fields)
                if event["event_type"] not in RELAYED_RUNTIME_EVENT_TYPES:
                    continue
                if event["subscription_id"] != subscription_id:
                    continue
                return (
                    {
                        "event": {
                            "eventType": event["event_type"],
                            "subscriptionId": event["subscription_id"],
                            "thingId": event["thing_id"],
                            "name": event["name"],
                            "timestamp": event["timestamp"],
                            "value": decode_runtime_payload(
                                event["payload_base64"], event["content_type"]
                            ),
                        }
                    },
                    current_cursor,
                )
