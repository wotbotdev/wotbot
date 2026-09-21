"""Exercise the production validator on real generated documents, without CDNs."""

import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from wotbot.panels import browser_validator as validator
from wotbot.panels.render import wrap_panel_document

pytestmark = pytest.mark.browser


def document(body="<h1 id='result'>Ready</h1>", script=""):
    return wrap_panel_document(
        body + "<script>" + script + "</script>", data={"data": '{"value":42}'}
    )


READY = "panelChecks({'rendered': () => document.querySelector('h1') !== null});"


@pytest.mark.external
def test_real_leaflet_attachment_panel_passes_production_validator():
    spec = importlib.util.spec_from_file_location(
        "panel_data_serve", Path(__file__).with_name("serve.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = validator.inspect_document(module.data_panel().body.decode())
    assert result["status"] == "passed", result["diagnostics"]


def test_exact_wrapped_data_renders_and_produces_a_screenshot(tmp_path):
    from PIL import Image

    result = validator.inspect_document(
        document(
            script="""
        document.querySelector('h1').textContent = panelData.read('data').value;
        panelChecks({'value matches attachment': () =>
            document.querySelector('h1').textContent === String(panelData.read('data').value)});
    """
        )
    )
    assert result["status"] == "passed", result["diagnostics"]
    assert result["checks"]["checks"][0]["passed"] is True
    png = base64.b64decode(result["screenshot_base64"])
    assert png.startswith(b"\x89PNG")
    (tmp_path / "panel.png").write_bytes(png)
    assert Image.open(io.BytesIO(png)).size == (1000, 650)
    narrow = base64.b64decode(result["narrow_screenshot_base64"])
    assert Image.open(io.BytesIO(narrow)).size == (390, 650)


def test_narrow_resize_observes_javascript_errors_without_reinitializing():
    result = validator.inspect_document(
        document(
            script=READY
            + """
        addEventListener('resize', () => { if (innerWidth < 500) throw Error('narrow layout crashed'); });
    """
        )
    )
    assert result["status"] == "failed"
    assert "narrow layout crashed" in json.dumps(result["diagnostics"])


@pytest.mark.parametrize(
    "script, expected",
    [
        ("missingLibrary.render();", "missingLibrary"),
        ("Promise.reject(new Error('async failure'));" + READY, "async failure"),
        ("panelData.read('misspelled');", "Unknown panel data attachment"),
        ("console.error('Rendering failed');" + READY, "Rendering failed"),
        ("panelChecks({'ranking matches': () => false});", "self_check"),
        ("panelChecks({});", "self_check"),
        (READY + "setTimeout(() => {throw Error('late failure')}, 100);", "late failure"),
    ],
)
def test_runtime_failures_block_publication(script, expected):
    result = validator.inspect_document(document(script=script), timeout_ms=2000)
    assert result["status"] == "failed", result["diagnostics"]
    assert expected in json.dumps(result["diagnostics"])


def test_error_free_blank_render_is_rejected_even_with_passing_checks():
    result = validator.inspect_document(
        document(body="<h1 style='display:none'>Invisible</h1>", script=READY)
    )
    assert result["status"] == "failed"
    assert any(item["kind"] == "blank" for item in result["diagnostics"])


@pytest.mark.parametrize(
    "script", ["", "panelChecks({'never settles': () => new Promise(() => {})});"]
)
def test_missing_or_unsettled_readiness_is_inconclusive(script):
    result = validator.inspect_document(document(script=script), timeout_ms=250)
    assert result["status"] == "inconclusive"
    assert any(item["kind"] == "readiness" for item in result["diagnostics"])


def test_authored_code_cannot_publish_a_pass_without_running_checks():
    result = validator.inspect_document(
        document(
            script="""
            window.panelChecksResult = {
                status: 'passed', checks: [{label: 'invented', passed: true, error: null}]
            };
        """
        ),
        timeout_ms=250,
    )
    assert result["status"] == "inconclusive"
    assert result["checks"] is None
    assert any(item["kind"] == "readiness" for item in result["diagnostics"])


def test_csp_blocks_internal_requests_without_contacting_a_backend():
    result = validator.inspect_document(
        document(
            script="""
        fetch('http://127.0.0.1:8123/api/things').catch(() => {});
    """
            + READY
        )
    )
    assert result["status"] == "failed"
    assert "Content Security Policy" in json.dumps(result["diagnostics"])


@pytest.mark.parametrize(
    "operation", ["writeProperty", "invokeAction", "observeProperty", "subscribeEvent"]
)
def test_non_read_bridge_calls_are_untested_without_reaching_devices(operation):
    argument = "() => {}" if operation in {"observeProperty", "subscribeEvent"} else "true"
    result = validator.inspect_document(
        document(
            script=f"""
        wot.{operation}('urn:lamp', 'power', {argument}).catch(() => {{}});
    """
            + READY
        )
    )
    assert result["status"] == "inconclusive"
    assert operation in json.dumps(result["diagnostics"])
    assert operation in result["untested_operations"]


def test_dependency_outage_is_inconclusive_instead_of_a_code_failure(monkeypatch):
    from playwright.sync_api import Error, Route

    def unavailable(*args, **kwargs):
        raise Error("Dependency connection timed out")

    monkeypatch.setattr(Route, "fetch", unavailable)
    result = validator.inspect_document(
        document(
            body='<script src="https://unpkg.com/example.js"></script><h1>Chart</h1>',
            script="missingLibrary.render();",
        )
    )
    assert result["status"] == "inconclusive"
    assert "dependency_unavailable" in json.dumps(result["diagnostics"])


def test_redirect_cannot_reach_internal_services(monkeypatch):
    from playwright.sync_api import Route

    requested = []

    class Redirect:
        status = 302

        def __init__(self):
            self.headers = {"location": "http://127.0.0.1:8123/api/things"}

        def dispose(self):
            pass

    def redirect(self, **kwargs):
        requested.append(kwargs["url"])
        assert kwargs["max_redirects"] == 0
        return Redirect()

    monkeypatch.setattr(Route, "fetch", redirect)
    result = validator.inspect_document(
        document(
            body='<script src="https://unpkg.com/example.js"></script><h1>Chart</h1>', script=READY
        )
    )
    assert result["status"] == "failed"
    assert requested == ["https://unpkg.com/example.js"]
    assert "outside the allowlist" in json.dumps(result["diagnostics"])


def test_hard_deadline_kills_a_stuck_renderer_and_next_request_works(monkeypatch):
    monkeypatch.setattr(validator, "DEADLINE_SECONDS", 3)
    result = asyncio.run(
        validator.validate(validator.ValidationRequest(html=document(script="while (true) {}")))
    )
    assert result["status"] == "inconclusive"
    assert result["diagnostics"][0]["kind"] == "timeout"
    monkeypatch.setattr(validator, "DEADLINE_SECONDS", 15)
    result = asyncio.run(
        validator.validate(validator.ValidationRequest(html=document(script=READY)))
    )
    assert result["status"] == "passed", result
