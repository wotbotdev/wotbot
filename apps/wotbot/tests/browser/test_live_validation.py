"""Backend -> WebSocket -> worker process -> Chromium -> WoT bridge round trips.

Only the runtime client is faked: no real devices or credentials are used.
"""

import asyncio
import base64
import json
import socket
import threading
import time
from unittest.mock import AsyncMock

import pytest
import uvicorn

pytest.importorskip("playwright")

from wotbot.core.settings import Settings
from wotbot.panels import browser, browser_validator
from wotbot.panels.render import wrap_panel_document

pytestmark = pytest.mark.browser
CAPABILITIES = [{"thingId": "urn:sensor", "affordances": ["reading"], "ops": ["readProperty"]}]


@pytest.fixture(scope="module")
def validator_url():
    # Keep the socket reserved until uvicorn owns it, avoiding a free-port race.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                browser_validator.app,
                log_level="warning",
                ws="websockets-sansio",
                ws_max_size=128 * 1024 * 1024,
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Validation service did not start")
            time.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture
def runtime(monkeypatch):
    client = AsyncMock()
    client.read_property.return_value = {
        "result": {"success": True, "payload": {"data": {"value": 21.5, "unit": "C"}}}
    }
    monkeypatch.setattr(browser, "WotRuntimeClient", lambda _settings: client)
    return client


def run(validator_url, source, capabilities=CAPABILITIES):
    document = wrap_panel_document(
        "<h1>Loading</h1><script>(async () => {" + source + "})();</script>",
        data={"reference": '{"expected":21.5}'},
    )
    settings = Settings(
        _env_file=None,
        panel_validator_url=validator_url,
        wot_runtime_api_token="test-runtime-secret",
    )
    return asyncio.run(browser.validate_in_browser(document, settings, capabilities=capabilities))


def test_live_and_attached_values_render_without_sending_credentials(
    validator_url, runtime, monkeypatch
):
    received = []
    original = browser_validator._run_worker

    async def capture(request, **kwargs):
        received.append(request.model_dump())
        return await original(request, **kwargs)

    monkeypatch.setattr(browser_validator, "_run_worker", capture)
    verdict = run(
        validator_url,
        """
        const value = await wot.readProperty('urn:sensor', 'reading', {uriVariables:{floor:2}});
        document.querySelector('h1').textContent = `${value.value} ${value.unit}`;
        await panelChecks({
            'live value agrees with attachment': () => value.value === panelData.read('reference').expected,
            'rendered': () => document.querySelector('h1').textContent === '21.5 C'
        });
    """,
    )
    assert verdict.status == "passed", verdict.model_dump()
    assert verdict.read_count == 1
    assert verdict.screenshot_base64
    assert "test-runtime-secret" not in json.dumps(received)
    assert set(received[0]) == {"html", "capabilities"}
    runtime.read_property.assert_awaited_once_with(
        thing_id="urn:sensor", property_name="reading", uri_variables={"floor": 2}
    )


def test_actual_read_shape_errors_are_caught(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        const value = await wot.readProperty('urn:sensor', 'reading');
        document.querySelector('h1').textContent = value.result.payload.data;
        await panelChecks({'rendered': () => true});
    """,
    )
    assert verdict.status == "failed", verdict.model_dump()
    assert any(item["kind"] == "javascript" for item in verdict.diagnostics)


def test_large_binary_read_survives_the_entire_bridge(validator_url, runtime):
    content = b"\x01" * 100_000
    runtime.read_property.return_value = {
        "result": {
            "success": True,
            "payload": {
                "kind": "binary",
                "content_type": "application/octet-stream",
                "body_base64": base64.b64encode(content).decode(),
                "size_bytes": len(content),
            },
        }
    }
    verdict = run(
        validator_url,
        """
        const value = await wot.readProperty('urn:sensor', 'reading');
        const bytes = wot.binaryToBytes(value);
        document.querySelector('h1').textContent = `${bytes.length} bytes`;
        await panelChecks({'binary survived': () =>
            value.contentType === 'application/octet-stream' && value.sizeBytes === 100000 &&
            bytes.length === 100000 && bytes[99999] === 1});
    """,
    )
    assert verdict.status == "passed", verdict.model_dump()
    assert verdict.read_count == 1
    assert "bodyBase64" not in json.dumps(verdict.model_dump())


def test_concurrent_property_reads_finish_before_render_checks(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        const values = await Promise.all([1, 2].map(floor =>
            wot.readProperty('urn:sensor', 'reading', {uriVariables:{floor}})));
        document.querySelector('h1').textContent = values.map(v => v.value).join(', ');
        await panelChecks({'two readings rendered': () =>
            document.querySelector('h1').textContent === '21.5, 21.5'});
    """,
    )
    assert verdict.status == "passed", verdict.model_dump()
    assert verdict.read_count == 2
    assert runtime.read_property.await_count == 2


def test_panel_cannot_call_the_host_binding_directly(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        await window.__panelValidationRead({op:'readProperty', thingId:'urn:sensor', name:'reading'});
        await panelChecks({'rendered': () => true});
    """,
    )
    assert verdict.status == "failed", verdict.model_dump()
    assert "trusted host" in json.dumps(verdict.diagnostics)
    assert runtime.mock_calls == []


def test_runtime_outage_is_inconclusive_even_if_the_panel_handles_it(validator_url, runtime):
    runtime.read_property.side_effect = ValueError("Device is offline")
    verdict = run(
        validator_url,
        """
        try { await wot.readProperty('urn:sensor', 'reading'); } catch {}
        await panelChecks({'rendered': () => true});
    """,
    )
    assert verdict.status == "inconclusive", verdict.model_dump()
    assert "Device is offline" in json.dumps(verdict.diagnostics)


def test_computed_undeclared_reads_are_rejected_without_contacting_runtime(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        const property = ['sec', 'ret'].join('');
        try { await wot.readProperty('urn:sensor', property); } catch {}
        await panelChecks({'rendered': () => true});
    """,
    )
    assert verdict.status == "failed", verdict.model_dump()
    assert "Not permitted" in json.dumps(verdict.diagnostics)
    assert runtime.mock_calls == []


def test_declared_action_during_initialization_is_blocked_and_untested(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        try { await wot.invokeAction('urn:sensor', 'reset'); } catch {}
        await panelChecks({'rendered': () => true});
    """,
        capabilities=[{"thingId": "urn:sensor", "affordances": ["reset"], "ops": ["invokeAction"]}],
    )
    assert verdict.status == "inconclusive", verdict.model_dump()
    assert "invokeAction" in verdict.untested_operations
    assert runtime.mock_calls == []


def test_action_control_is_not_clicked_and_can_pass_initialization(validator_url, runtime):
    verdict = run(
        validator_url,
        """
        document.querySelector('h1').onclick = () => wot.invokeAction('urn:sensor', 'reset');
        await panelChecks({'control rendered': () => document.querySelector('h1') !== null});
    """,
        capabilities=[{"thingId": "urn:sensor", "affordances": ["reset"], "ops": ["invokeAction"]}],
    )
    assert verdict.status == "passed", verdict.model_dump()
    assert verdict.read_count == 0
    assert "invokeAction" in verdict.untested_operations
    assert runtime.mock_calls == []
