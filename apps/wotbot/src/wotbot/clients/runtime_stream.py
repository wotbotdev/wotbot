"""Shared subscription event polling for external agent and panel clients."""

import re
import time
from typing import Any

import redis.asyncio as redis

from wotbot.jobs.stream import parse_runtime_stream_fields
from wotbot.runtime_events import decode_runtime_payload

RELAYED_RUNTIME_EVENT_TYPES = {"property_observed", "event_received"}
_CURSOR = re.compile(r"(\d+-\d+|0)")

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


def requested_cursor(arguments: dict[str, Any]) -> str | None:
    """Validate the stream position the caller wants to resume from.

    Replaying from an older position only re-delivers events this caller was
    already entitled to, so the value needs shape checking, not authorization.
    ``None`` means the caller has no position and should start from the tail.
    """
    cursor = arguments.get("cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
        raise ValueError("cursor must be a Redis stream ID")
    return cursor


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
