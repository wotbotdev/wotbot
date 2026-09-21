import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wotbot.core.settings import Settings
from wotbot.panels.browser import ReadOnlyBridge, validate_in_browser
from wotbot.panels.browser_protocol import MAX_READS
from wotbot.panels.browser_validator import POLICY, allowed_url

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aE1sAAAAASUVORK5CYII="
PASSED = {
    "status": "passed",
    "checks": {
        "status": "passed",
        "checks": [{"label": "rendered", "passed": True, "error": None}],
    },
    "screenshot_base64": PNG,
    "narrow_screenshot_base64": PNG,
}


def test_browser_policy_matches_the_ui_csp():
    ui = Path(__file__).resolve().parents[2] / "ui"
    runner = ui / "node_modules/.bin/tsx"
    if not runner.exists():
        pytest.skip("Install the UI dependencies to check CSP parity")
    csp = subprocess.check_output(
        [
            "node",
            "--import",
            "tsx",
            "-e",
            "const { PANEL_CSP } = require('./src/lib/panel-csp.ts'); process.stdout.write(PANEL_CSP);",
        ],
        cwd=ui,
        text=True,
        timeout=10,
    )
    actual = {
        parts[0]: set(parts[1:]) for directive in csp.split(";") if (parts := directive.split())
    }
    assert actual == {name: set(values) for name, values in POLICY.items()}


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.jsdelivr.net/lib.js",
        "https://cdn.jsdelivr.net.evil.example/lib.js",
        "https://localhost/lib.js",
        "https://127.0.0.1/lib.js",
        "https://cdn.jsdelivr.net:8123/lib.js",
        "https://user:password@cdn.jsdelivr.net/lib.js",
        "https://evil.example/redirect?to=https://unpkg.com/lib.js",
    ],
)
def test_disallowed_requests_are_never_sent(url):
    assert not allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.jsdelivr.net/lib.js",
        "https://a.tile.openstreetmap.org/1/2/3.png",
    ],
)
def test_declared_dependencies_are_allowed(url):
    assert allowed_url(url)


def response_result(response):
    def respond(request):
        assert request.url == "http://validator/validate"
        assert json.loads(request.content) == {"html": "<h1>Panel</h1>"}
        assert "authorization" not in request.headers
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("wotbot.panels.browser.httpx.AsyncClient", return_value=client):
        return asyncio.run(
            validate_in_browser(
                "<h1>Panel</h1>", Settings(_env_file=None, panel_validator_url="http://validator")
            )
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json={"status": "passed"}),
        httpx.Response(200, json={"status": "passed", "checks": {"status": "failed"}}),
        httpx.Response(200, json={**PASSED, "checks": {"status": "passed", "checks": []}}),
        httpx.Response(
            200,
            json={
                **PASSED,
                "checks": {
                    "status": "passed",
                    "checks": [{"label": "rendered", "passed": False, "error": "Not ready"}],
                },
            },
        ),
        httpx.Response(200, json={**PASSED, "screenshot_base64": None}),
        httpx.Response(200, json={**PASSED, "narrow_screenshot_base64": "not a PNG"}),
        httpx.Response(200, json={"status": "unknown"}),
    ],
)
def test_outage_or_invalid_response_cannot_be_a_pass(response):
    result = response_result(response)
    assert result.status == "unavailable"
    assert "do not rewrite" in result.diagnostics[0]["message"]


def test_success_carries_checks_without_copying_screenshots_into_model_context():
    result = response_result(httpx.Response(200, json=PASSED))
    assert result.status == "passed"
    assert result.screenshot_base64 == PNG
    assert result.narrow_screenshot_base64 == PNG
    assert "screenshot_base64" not in result.model_dump()
    assert "narrow_screenshot_base64" not in result.model_dump()


CAPABILITIES = [{"thingId": "urn:sensor", "affordances": ["reading"], "ops": ["readProperty"]}]
READ = {"op": "readProperty", "thingId": "urn:sensor", "name": "reading"}


@pytest.mark.parametrize("value", [None, False, 0, "", [1, 2], {"value": 21.5, "unit": "C"}])
def test_read_bridge_returns_the_actual_decoded_value(value):
    runtime = AsyncMock()
    runtime.read_property.return_value = {"result": {"success": True, "payload": {"data": value}}}
    bridge = ReadOnlyBridge(CAPABILITIES, runtime)
    result = asyncio.run(bridge.read({**READ, "uriVariables": {"floor": 2}, "form_index": 99}))
    assert result == {"ok": True, "result": value}
    runtime.read_property.assert_awaited_once_with(
        thing_id="urn:sensor", property_name="reading", uri_variables={"floor": 2}
    )


def test_read_bridge_preserves_binary_values_in_the_ui_shape():
    runtime = AsyncMock()
    runtime.read_property.return_value = {
        "result": {
            "success": True,
            "payload": {
                "kind": "binary",
                "body_base64": "AQI=",
                "content_type": "image/png",
                "size_bytes": 2,
            },
        }
    }
    result = asyncio.run(ReadOnlyBridge(CAPABILITIES, runtime).read(READ))
    assert result == {
        "ok": True,
        "result": {
            "kind": "binary",
            "bodyBase64": "AQI=",
            "contentType": "image/png",
            "sizeBytes": 2,
        },
    }


@pytest.mark.parametrize(
    "bridge_request",
    [
        {**READ, "op": "writeProperty", "value": True},
        {**READ, "op": "invokeAction"},
        {**READ, "op": "observeProperty"},
        {**READ, "op": "subscribeEvent"},
        {**READ, "thingId": "urn:undeclared"},
        {**READ, "name": "undeclared"},
        {**READ, "uriVariables": "invalid"},
    ],
)
def test_backend_rechecks_requests_without_trusting_the_validator(bridge_request):
    runtime = AsyncMock()
    result = asyncio.run(ReadOnlyBridge(CAPABILITIES, runtime).read(bridge_request))
    assert result["ok"] is False
    assert runtime.mock_calls == []


def test_read_permission_is_not_implied_by_write_permission():
    runtime = AsyncMock()
    caps = [{"thingId": "urn:sensor", "affordances": [], "ops": ["writeProperty"]}]
    result = asyncio.run(ReadOnlyBridge(caps, runtime).read(READ))
    assert result["kind"] == "capability"
    assert runtime.mock_calls == []


def test_empty_affordances_allow_any_declared_property():
    runtime = AsyncMock()
    runtime.read_property.return_value = {"result": {"success": True, "payload": {"data": 4}}}
    caps = [{"thingId": "urn:sensor", "affordances": [], "ops": ["readProperty"]}]
    assert asyncio.run(ReadOnlyBridge(caps, runtime).read(READ))["ok"] is True


def test_read_failure_and_limit_do_not_trigger_device_mutations():
    runtime = AsyncMock()
    runtime.read_property.side_effect = ValueError("Device offline")
    bridge = ReadOnlyBridge(CAPABILITIES, runtime)
    assert asyncio.run(bridge.read(READ)) == {
        "ok": False,
        "kind": "read_unavailable",
        "error": "Device offline",
    }
    bridge.calls = MAX_READS
    assert asyncio.run(bridge.read(READ))["kind"] == "read_limit"
    runtime.read_property.assert_awaited_once()
    runtime.write_property.assert_not_called()
    runtime.invoke_action.assert_not_called()


def test_runtime_failure_envelope_is_not_delivered_as_success():
    runtime = AsyncMock()
    runtime.read_property.return_value = {"result": {"success": False, "status_text": "Offline"}}
    result = asyncio.run(ReadOnlyBridge(CAPABILITIES, runtime).read(READ))
    assert result == {"ok": False, "kind": "read_unavailable", "error": "Offline"}


def test_empty_runtime_payload_matches_the_ui_undefined_value():
    runtime = AsyncMock()
    runtime.read_property.return_value = {"result": {"success": True}}
    assert asyncio.run(ReadOnlyBridge(CAPABILITIES, runtime).read(READ)) == {"ok": True}


def test_unavailable_live_validation_cannot_pass(monkeypatch):
    async def disconnected(*args):
        raise TimeoutError("Lost validation connection")

    monkeypatch.setattr("wotbot.panels.browser._validate_live", disconnected)
    result = asyncio.run(
        validate_in_browser("<h1>Panel</h1>", Settings(_env_file=None), capabilities=CAPABILITIES)
    )
    assert result.status == "unavailable"
