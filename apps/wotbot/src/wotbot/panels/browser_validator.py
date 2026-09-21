"""Browser worker with a read-only relay to its caller; no runtime credentials.

Run with uvicorn wotbot.panels.browser_validator:app. Each request runs in a
fresh process/browser; the service kills its process group at the hard deadline,
including when JavaScript blocks the renderer forever. No panel files persist.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from wotbot.panels.browser_protocol import (
    MAX_DOCUMENT_MESSAGE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_READS,
    UNTESTED_OPERATIONS,
    request_problem,
)
from wotbot.panels.evidence import checks_passed

POLICY = json.loads(Path(__file__).with_name("browser_policy.json").read_text())
CSP = "; ".join(f"{name} {' '.join(values)}" for name, values in POLICY.items())
HOST_URL = "https://panel-host.invalid/"
PANEL_URL = "https://panel-document.invalid/"
DEADLINE_SECONDS = 25
MAX_DOCUMENT_CHARS = 64 * 1024 * 1024  # 8 MiB JSON may expand through escaping.
MAX_DIAGNOSTICS = 20
logger = logging.getLogger(__name__)


def allowed_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = parsed.hostname or ""
    hosts = {urlsplit(item).hostname for item in POLICY["connect-src"] if item.startswith("https:")}
    return host in hosts or host.endswith(".tile.openstreetmap.org")


def inspect_document(
    document: str,
    *,
    timeout_ms: int = 15_000,
    capabilities: list[dict] | None = None,
    read_property: Callable[[dict], dict] | None = None,
) -> dict:
    """Execute the submitted wrapper and capture a bounded initialization verdict."""
    from PIL import Image
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    diagnostics: list[dict] = []
    unavailable_dependencies = False
    checks = None
    screenshot = None
    narrow_screenshot = None
    observing = True
    read_count = 0
    read_attempts = 0

    def report(kind, message):
        if observing and len(diagnostics) < MAX_DIAGNOSTICS:
            item = {"kind": kind, "message": str(message)[:1000]}
            if item not in diagnostics:
                diagnostics.append(item)

    with sync_playwright() as playwright:
        # Keep Chromium's sandbox enabled. Container user namespaces must work;
        # a launch failure is infrastructure failure, never a validation pass.
        browser = playwright.chromium.launch(chromium_sandbox=True)
        context = browser.new_context(
            viewport={"width": 1000, "height": 650},
            service_workers="block",
            accept_downloads=False,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.on("pageerror", lambda error: report("javascript", error))
        page.on(
            "requestfailed", lambda request: report("resource", f"{request.failure}: {request.url}")
        )
        page.on("console", lambda msg: report("console", msg.text) if msg.type == "error" else None)
        page.on("dialog", lambda dialog: (report("dialog", dialog.message), dialog.dismiss()))
        context.route_web_socket(
            "**/*",
            lambda ws: (
                report("blocked_request", f"WebSocket is not permitted: {ws.url}"),
                ws.close(),
            ),
        )

        def bridge_request(source, request):
            nonlocal read_count, read_attempts
            if not observing:
                return {"ok": False, "error": "Validation has finished"}
            if source["frame"] != page.main_frame:
                report("capability", "Only the trusted host may relay validation reads")
                return {"ok": False, "error": "Invalid validation bridge caller"}
            problem = request_problem(request, capabilities or [])
            if problem:
                report(*problem)
                return {"ok": False, "error": problem[1]}
            if read_attempts >= MAX_READS:
                report("read_limit", "Validation property-read limit reached")
                return {"ok": False, "error": "Validation property-read limit reached"}
            read_attempts += 1
            if read_property is None:
                report("read_unavailable", "No live property-read relay is available")
                return {"ok": False, "error": "Live property reads are unavailable"}
            reply = read_property(request)
            if reply.get("ok") is True:
                read_count += 1
            else:
                report(reply.get("kind", "read_unavailable"), reply.get("error", "Read failed"))
            return reply

        page.expose_binding("__panelValidationRead", bridge_request)

        served_documents: set[str] = set()

        def route_request(route):
            nonlocal unavailable_dependencies
            request = route.request
            url = request.url
            if request.is_navigation_request() and url in {HOST_URL, PANEL_URL}:
                # Only the initial host and panel documents can be navigated to.
                is_host = url == HOST_URL and request.frame == page.main_frame
                is_panel = url == PANEL_URL and request.frame.parent_frame == page.main_frame
                if (
                    (is_host or is_panel)
                    and request.method == "GET"
                    and url not in served_documents
                ):
                    served_documents.add(url)
                    host = (
                        "<style>body{margin:0}iframe{display:block;width:100vw;height:650px;border:0}</style>"
                        f'<iframe id="panel" sandbox="allow-scripts allow-same-origin" '
                        f'src="{PANEL_URL}"></iframe>'
                        '<script>addEventListener("message", async e => {'
                        'if(e.source !== document.querySelector("iframe").contentWindow || '
                        f'e.origin !== "{PANEL_URL.rstrip("/")}" || '
                        'e.data?.source !== "wot-bridge") return;'
                        "const reply = await window.__panelValidationRead(e.data);"
                        'e.source.postMessage({...reply, source:"wot-bridge-host", id:e.data.id}, '
                        f'"{PANEL_URL.rstrip("/")}");'
                        "});</script>"
                    )
                    route.fulfill(
                        status=200,
                        body=host if is_host else document,
                        headers={
                            "Content-Type": "text/html; charset=utf-8",
                            **({} if is_host else {"Content-Security-Policy": CSP}),
                        },
                    )
                    return
            if request.is_navigation_request() or request.method != "GET" or not allowed_url(url):
                report("blocked_request", f"Request is not permitted: {url}")
                route.abort()
                return
            try:
                # Fetch redirects ourselves: route.continue_ does not intercept
                # every redirected request. Never contact a non-allowlisted host.
                for _ in range(6):
                    if not allowed_url(url):
                        report(
                            "blocked_request", f"Dependency redirected outside the allowlist: {url}"
                        )
                        route.abort()
                        return
                    response = route.fetch(url=url, max_redirects=0, timeout=5000)
                    if 300 <= response.status < 400 and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        response.dispose()
                        continue
                    if response.status >= 400:
                        unavailable_dependencies |= response.status == 429 or response.status >= 500
                        report("resource", f"HTTP {response.status}: {url}")
                    route.fulfill(response=response)
                    response.dispose()
                    return
                raise RuntimeError("Too many dependency redirects")
            except (Error, RuntimeError) as error:
                unavailable_dependencies = True
                report("dependency_unavailable", f"{url}: {error}")
                route.abort()

        context.route("**/*", route_request)
        try:
            page.goto(HOST_URL, wait_until="load")
            frame = page.frame(url=PANEL_URL)
            if frame is None:
                report("navigation", "Panel did not remain in its validation frame")
            else:
                # panelChecks is the readiness signal, declared after rendering.
                # Keep pumping browser events even when a panel logs an error.
                deadline = time.monotonic() + timeout_ms / 1000
                while time.monotonic() < deadline:
                    checks = frame.evaluate("() => window.panelChecksResult || null")
                    if checks is not None or diagnostics:
                        break
                    page.wait_for_timeout(100)
                if checks is None and not diagnostics:
                    report("readiness", "No completed panelChecks verdict before the deadline")
                if checks is not None and not checks_passed(checks):
                    report("self_check", "Panel self-checks failed or declared no assertions")
                # Observe errors queued just after the readiness signal, too.
                page.wait_for_timeout(1000)
                screenshot = page.screenshot(type="png", timeout=3000)
                with Image.open(io.BytesIO(screenshot)) as pixels:
                    # A deliberately narrow check: uniform pixels are blank.
                    # This is not a judgement of layout, map tiles or correctness.
                    if all(high - low < 3 for low, high in pixels.convert("RGB").getextrema()):
                        report("blank", "Panel rendered a blank, uniform viewport")
                # Resize the same initialized panel: no reload or duplicate initial
                # property reads. Keep observing resize handlers and lazy resources.
                page.set_viewport_size({"width": 390, "height": 650})
                page.wait_for_timeout(1000)
                narrow_screenshot = page.screenshot(type="png", timeout=3000)
        except TimeoutError:
            report("timeout", "Panel did not finish loading or rendering before the deadline")
        except Error as error:
            report("browser", error)
        finally:
            # Closing a page aborts pending resources; teardown errors are not
            # evidence that initialization failed during the observation window.
            observing = False
            browser.close()

    status = "passed"
    if diagnostics:
        status = (
            "inconclusive"
            if unavailable_dependencies
            or any(
                item["kind"]
                in {
                    "timeout",
                    "readiness",
                    "browser",
                    "bridge_blocked",
                    "read_unavailable",
                    "read_limit",
                }
                for item in diagnostics
            )
            else "failed"
        )
    return {
        "status": status,
        "diagnostics": diagnostics,
        "checks": checks,
        "read_count": read_count,
        "untested_operations": UNTESTED_OPERATIONS,
        "screenshot_base64": base64.b64encode(screenshot).decode() if screenshot else None,
        "narrow_screenshot_base64": (
            base64.b64encode(narrow_screenshot).decode() if narrow_screenshot else None
        ),
    }


app = FastAPI(docs_url=None, redoc_url=None)
_slot = asyncio.Semaphore(1)


class ValidationRequest(BaseModel):
    html: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    capabilities: list[dict] = Field(default_factory=list)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/validate")
async def validate(request: ValidationRequest):
    return await _run_worker(request)


@app.websocket("/validate-live")
async def validate_live(socket: WebSocket):
    await socket.accept()

    async def relay(message):
        await socket.send_json(message)
        reply = await socket.receive_json()
        if not isinstance(reply, dict) or reply.get("id") != message["id"]:
            raise ValueError("Mismatched validation read response")
        return reply

    try:
        async with asyncio.timeout(5):
            request = ValidationRequest.model_validate(await socket.receive_json())
        result = await _run_worker(request, relay=relay)
        await socket.send_json({"type": "result", "result": result})
    except WebSocketDisconnect:
        return
    except (HTTPException, TimeoutError, ValueError):
        with suppress(WebSocketDisconnect):
            await socket.send_json(
                {
                    "type": "result",
                    "result": {
                        "status": "unavailable",
                        "diagnostics": [
                            {
                                "kind": "validator_unavailable",
                                "message": "Live browser validation is unavailable",
                            }
                        ],
                    },
                }
            )
    finally:
        with suppress(WebSocketDisconnect, RuntimeError):
            await socket.close()


async def _run_worker(
    request: ValidationRequest, *, relay: Callable[[dict], Awaitable[dict]] | None = None
):
    import psutil

    # No unbounded queue of browser processes or in-memory datasets.
    if _slot.locked():
        raise HTTPException(status_code=503, detail="Panel validator is busy")
    async with _slot:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            __file__,
            "--worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_MESSAGE_BYTES,
        )
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                payload = {**request.model_dump(), "live": relay is not None}
                process.stdin.write((json.dumps(payload) + "\n").encode())
                await process.stdin.drain()
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if message.get("type") == "result":
                        process.stdin.close()
                        await process.wait()
                        if process.returncode == 0:
                            return message["result"]
                        break
                    if message.get("type") != "read" or relay is None:
                        raise ValueError("Unexpected browser worker message")
                    reply = await relay(message)
                    process.stdin.write((json.dumps(reply) + "\n").encode())
                    await process.stdin.drain()
                logger.warning(
                    "Panel browser worker failed: %s",
                    (await process.stderr.read(2000)).decode(errors="replace"),
                )
                raise HTTPException(status_code=503, detail="Browser worker failed")
        except TimeoutError:
            return {
                "status": "inconclusive",
                "diagnostics": [
                    {"kind": "timeout", "message": "Browser validation exceeded its hard deadline"}
                ],
            }
        finally:
            # Chromium starts a separate process group. Enumerate descendants
            # before killing their parent so a hung browser cannot be orphaned.
            with suppress(psutil.NoSuchProcess):
                for child in reversed(psutil.Process(process.pid).children(recursive=True)):
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


if __name__ == "__main__" and "--worker" in sys.argv:
    submitted = json.loads(sys.stdin.readline(MAX_DOCUMENT_MESSAGE_BYTES + 1))
    request_id = 0

    def read_from_caller(request):
        global request_id
        request_id += 1
        print(json.dumps({"type": "read", "id": request_id, "request": request}), flush=True)
        reply = json.loads(sys.stdin.readline(MAX_MESSAGE_BYTES + 1))
        if reply.get("id") != request_id:
            raise ValueError("Mismatched property read response")
        return reply

    result = inspect_document(
        submitted["html"],
        capabilities=submitted.get("capabilities", []),
        read_property=read_from_caller if submitted.get("live") else None,
    )
    print(json.dumps({"type": "result", "result": result}), flush=True)
