"""Headless runs of the panel acceptance fixtures that `serve.py` hosts.

These are the checks `README.md` previously asked a person to read off the page.
The fixtures already assert against the real wrapper, the real `window.wot`
bridge and the real `window.panelData` accessor; all that was missing was
something to open them and report the result. Each fixture ends by writing a
terminal string into the page, so a run reduces to waiting for that string and
failing with whatever the page says instead.

Opt in with `-m browser`, after `pip install -e .[browser]` and
`playwright install chromium`. The data-panel fixture additionally loads Leaflet
from a CDN and OpenStreetMap tiles, so it carries `external` too.
"""

from __future__ import annotations

import importlib.util
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

pytestmark = pytest.mark.browser

pytest.importorskip("playwright", reason="install the browser extra to run panel acceptance")

from playwright.sync_api import Browser, Page, expect, sync_playwright
from playwright.sync_api import Error as PlaywrightError

HERE = Path(__file__).resolve().parent
STARTUP_TIMEOUT = 30.0
# Generous: a cold Chromium plus a CDN fetch is slow well before anything is wrong.
CHECK_TIMEOUT_MS = 30_000


def _serve_app():
    """Load the sibling fixture server without putting its directory on `sys.path`."""
    spec = importlib.util.spec_from_file_location("panel_acceptance_serve", HERE / "serve.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[str]:
    """The real `serve.py` app, on a loopback port, for the duration of the module."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(_serve_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Panel acceptance fixture server did not start")
        time.sleep(0.05)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _await_verdict(locator, passed: str, failed: str) -> str:
    """Wait for whichever terminal marker the fixture writes, and return the page text.

    Waiting only for the success string would spend the whole timeout on a panel
    that already reported a failure, and would report it as a hang. Those are
    different results: a fixture that never writes either marker still times out
    here, and that timeout keeps meaning "never finished" rather than "failed".
    """
    both = re.compile(f"{re.escape(passed)}|{re.escape(failed)}")
    expect(locator).to_contain_text(both, timeout=CHECK_TIMEOUT_MS)
    return locator.inner_text()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except PlaywrightError as error:
            # The package can be installed without the browser it drives, which is
            # a missing dependency rather than a failure, and is skipped for the
            # same reason the import above is. Other launch failures still raise.
            if "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("run `playwright install chromium` to run panel acceptance")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[tuple[Page, list[str]]]:
    """A page plus the uncaught errors it raised, which no fixture asserts on itself.

    A panel can report every check as passed and still have thrown somewhere the
    fixture never looks, so the errors are collected separately and asserted on
    by each test. This is the same signal pre-publication validation would read.
    """
    context = browser.new_context()
    opened = context.new_page()
    errors: list[str] = []
    opened.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield opened, errors
    finally:
        context.close()


def test_bridge_panel_reports_all_checks_passed(fixture_server, page):
    """`panel-fixture.html`, wrapped and sandboxed, against the host in `normal-host.html`."""
    opened, errors = page
    opened.goto(f"{fixture_server}/", wait_until="load")

    # The panel runs sandboxed in an opaque origin, so read it through the frame.
    checks = opened.frame_locator("#panel").locator("#checks")
    verdict = _await_verdict(checks, "ALL PANEL CHECKS PASSED", "FAIL ")
    assert "ALL PANEL CHECKS PASSED" in verdict, verdict
    assert errors == []


@pytest.mark.external
def test_data_panel_reports_all_attachment_checks_passed(fixture_server, page):
    """`data-panel.html`: attachment transport, snapshot isolation and exact geometry.

    Leaflet and the tile layer come from the network, so a failure here is not
    necessarily a panel defect. The assertions below cover the attachment
    behaviour; tile imagery is not asserted on and stays a manual check.
    """
    opened, errors = page
    opened.goto(f"{fixture_server}/data-panel", wait_until="load")

    status = opened.locator("#status")
    verdict = _await_verdict(status, "ALL ATTACHMENT CHECKS PASSED", "FAILED:")
    assert "ALL ATTACHMENT CHECKS PASSED" in verdict, verdict
    assert errors == []

    # Set only once the fixture's own checks have all passed; asserted here so a
    # fixture weakened into vacuous truth does not keep reporting success.
    summary = opened.evaluate("() => window.attachmentChecks")
    assert summary["exactGeometry"] is True
    assert summary["immutableSnapshot"] is True
    assert summary["holes"] == 1
    assert summary["bytes"] > 2000, "fixture must stay larger than the executor stdout limit"


def _declare(opened, body: str):
    """Run `panelChecks` inside a blank wrapped panel and return the verdict."""
    return opened.evaluate(f"async () => await window.panelChecks({body})")


def test_panel_checks_reports_each_declared_check(fixture_server, page):
    """Pass, fail, throw and async, in declaration order, from one call."""
    opened, errors = page
    opened.goto(f"{fixture_server}/blank-panel", wait_until="load")

    verdict = _declare(
        opened,
        """{
            'plain pass': () => 1 === 1,
            'zero is a value, not a blank': () => 0,
            'async pass': async () => true,
            'plain fail': () => 1 === 2,
            'throwing check': () => { throw new Error('ranking differs at 100m'); },
            'forgotten return': () => { const unused = 1; },
        }""",
    )

    assert verdict["status"] == "failed"
    assert [check["label"] for check in verdict["checks"]] == [
        "plain pass",
        "zero is a value, not a blank",
        "async pass",
        "plain fail",
        "throwing check",
        "forgotten return",
    ]
    passed = {check["label"]: check["passed"] for check in verdict["checks"]}
    assert passed["plain pass"] is True
    assert passed["zero is a value, not a blank"] is True
    assert passed["async pass"] is True
    assert passed["plain fail"] is False
    # A check that forgets to return must not pass: a check that cannot fail is
    # the failure mode this whole mechanism exists to avoid.
    assert passed["forgotten return"] is False

    errored = next(c for c in verdict["checks"] if c["label"] == "throwing check")
    assert errored["error"] == "ranking differs at 100m"
    assert errors == []


def test_panel_checks_publishes_the_verdict(fixture_server, page):
    """The settled verdict is readable from outside the panel, and is frozen."""
    opened, errors = page
    opened.goto(f"{fixture_server}/blank-panel", wait_until="load")

    # The result is protected before the first check, not just after completion.
    assert (
        opened.evaluate(
            "() => { window.panelChecksResult = {status:'passed'}; "
            "return window.panelChecksResult; }"
        )
        is None
    )

    _declare(opened, "{'a check': () => false}")

    assert opened.evaluate("() => window.panelChecksResult") == {
        "status": "failed",
        "checks": [{"label": "a check", "passed": False, "error": "check returned false"}],
    }
    # A panel must not be able to talk its way to a pass after the fact.
    assert (
        opened.evaluate(
            "() => { try { window.panelChecksResult = {status:'passed'}; } catch {} "
            "return window.panelChecksResult.status; }"
        )
        == "failed"
    )
    assert (
        opened.evaluate(
            "() => { try { window.panelChecksResult.checks[0].passed = true; } catch {} "
            "return window.panelChecksResult.checks[0].passed; }"
        )
        is False
    )
    assert errors == []


def test_panel_checks_distinguishes_declaring_nothing_from_passing(fixture_server, page):
    """An absent check is unknown, not satisfied."""
    opened, errors = page
    opened.goto(f"{fixture_server}/blank-panel", wait_until="load")

    assert _declare(opened, "{}")["status"] == "empty"
    assert opened.evaluate("() => window.panelChecksResult.status") == "empty"
    assert errors == []


def test_panel_checks_refuses_a_second_call(fixture_server, page):
    """The verdict is posted once, so a later set of checks cannot be reported."""
    opened, errors = page
    opened.goto(f"{fixture_server}/blank-panel", wait_until="load")

    assert _declare(opened, "{'first': () => true}")["status"] == "passed"
    with pytest.raises(PlaywrightError, match="already called"):
        _declare(opened, "{'second': () => false}")
    # The first verdict stands rather than being replaced by a partial one.
    assert opened.evaluate("() => window.panelChecksResult.checks.length") == 1
    # The refusal rejects the caller; it does not escape as an uncaught error.
    assert errors == []
