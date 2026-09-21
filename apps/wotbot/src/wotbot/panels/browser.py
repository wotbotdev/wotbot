"""Client for the isolated panel browser service; never silently skip validation."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import httpx

from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.settings import Settings
from wotbot.panels.browser_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_READS,
    request_problem,
)
from wotbot.panels.evidence import BrowserValidation, checks_passed, decode_screenshot

_UNDEFINED = object()


def _read_value(response: dict):
    """Match the UI bridge's decoded JSON/binary value, never a transport envelope."""
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("Runtime returned no property result")
    if result.get("success") is False:
        raise ValueError(result.get("status_text") or "Property read failed")
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return _UNDEFINED
    if payload.get("kind") == "binary":
        body = payload.get("bodyBase64", payload.get("body_base64"))
        if not isinstance(body, str):
            raise ValueError("Runtime returned an invalid binary property")
        value = {
            "kind": "binary",
            "contentType": payload.get(
                "contentType", payload.get("content_type", "application/octet-stream")
            ),
            "bodyBase64": body,
        }
        size = payload.get("sizeBytes", payload.get("size_bytes"))
        if isinstance(size, (int, float)) and not isinstance(size, bool):
            value["sizeBytes"] = size
        return value
    return payload.get("data", _UNDEFINED)


class ReadOnlyBridge:
    """Lives in the calling backend/agent process, which retains runtime credentials."""

    def __init__(self, capabilities: list[dict], runtime: WotRuntimeClient):
        self.capabilities = capabilities
        self.runtime = runtime
        self.calls = 0

    async def read(self, request: dict) -> dict:
        problem = request_problem(request, self.capabilities)
        if problem:
            return {"ok": False, "kind": problem[0], "error": problem[1]}
        if self.calls >= MAX_READS:
            return {
                "ok": False,
                "kind": "read_limit",
                "error": "Validation property-read limit reached",
            }
        self.calls += 1
        try:
            async with asyncio.timeout(5):
                response = await self.runtime.read_property(
                    thing_id=request["thingId"],
                    property_name=request["name"],
                    uri_variables=request.get("uriVariables"),
                )
            value = _read_value(response)
            reply = {"ok": True}
            if value is not _UNDEFINED:
                reply["result"] = value
            if len(json.dumps(reply).encode()) > MAX_MESSAGE_BYTES:
                raise ValueError("Property value exceeds the validation message limit")
            return reply
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as error:
            return {
                "ok": False,
                "kind": "read_unavailable",
                "error": str(error) or "Property read timed out",
            }


async def _validate_live(document: str, settings: Settings, capabilities: list[dict]) -> dict:
    bridge = ReadOnlyBridge(capabilities, WotRuntimeClient(settings))
    # This control connection belongs to the backend, never to the generated page.
    # No credentials, runtime URL or callback endpoint are sent to the validator.
    async with (
        asyncio.timeout(30),
        aiohttp.ClientSession() as client,
        client.ws_connect(
            f"{settings.panel_validator_url.rstrip('/')}/validate-live",
            max_msg_size=MAX_MESSAGE_BYTES,
        ) as socket,
    ):
        await socket.send_json({"html": document, "capabilities": capabilities})
        while True:
            message = await socket.receive_json()
            if not isinstance(message, dict):
                raise ValueError("Invalid validator message")
            if message.get("type") == "result":
                return message["result"]
            if message.get("type") != "read" or not isinstance(message.get("request"), dict):
                raise ValueError("Invalid validator read request")
            reply = await bridge.read(message["request"])
            await socket.send_json({"id": message.get("id"), **reply})


async def validate_in_browser(
    document: str, settings: Settings, *, capabilities: list[dict] | None = None
) -> BrowserValidation:
    try:
        if capabilities:
            response_body = await _validate_live(document, settings, capabilities)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{settings.panel_validator_url.rstrip('/')}/validate", json={"html": document}
                )
                response.raise_for_status()
                response_body = response.json()
        result = BrowserValidation.model_validate(response_body)
        if result.status == "passed" and (
            result.diagnostics
            or not checks_passed(result.checks)
            or not decode_screenshot(result.screenshot_base64)
            or not decode_screenshot(result.narrow_screenshot_base64)
        ):
            raise ValueError("Invalid successful browser verdict")
        return result
    except (httpx.HTTPError, aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
        return BrowserValidation(
            status="unavailable",
            diagnostics=[
                {
                    "kind": "validator_unavailable",
                    "message": "Browser validator is unavailable or returned an invalid response. "
                    "No panel was published; do not rewrite the panel for this outage.",
                }
            ],
        )
