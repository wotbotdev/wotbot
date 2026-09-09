"""Local acceptance fixtures only; no real credentials or device connections."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from wotbot.core.settings import Settings
from wotbot.mcp_apps.assets import router
from wotbot.mcp_apps.render import wrap_mcp_app_document
from wotbot.panels.render import wrap_panel_document

ROOT = Path(__file__).resolve().parent
app = FastAPI()
app.include_router(router)


@app.get("/")
def host():
    return FileResponse(ROOT / "mcp-host.html")


@app.get("/normal")
def normal_host():
    return FileResponse(ROOT / "normal-host.html")


@app.get("/proxy")
def proxy():
    return FileResponse(ROOT / "mcp-proxy.html")


@app.get("/host.js")
def host_js():
    return FileResponse(os.environ["MCP_HOST_BUNDLE"], media_type="text/javascript")


@app.get("/panel")
def panel():
    return HTMLResponse(
        wrap_mcp_app_document(
            (ROOT / "panel-fixture.html").read_text(),
            "Test panel",
            settings=Settings(registry_public_url="http://localhost:8918"),
        )
    )


@app.get("/normal-panel")
def normal_panel():
    return HTMLResponse(
        wrap_panel_document((ROOT / "panel-fixture.html").read_text(), "Test panel")
    )


# Minimal backend fixture for the real Next.js Panels drawer/deep-link check.
PANEL = {
    "id": "panel-fixture",
    "title": "A2A fixture panel",
    "capabilities": [],
    "source_thread_id": None,
    "created_at": "2026-09-09T00:00:00Z",
    "updated_at": "2026-09-09T00:00:00Z",
}
MARKUP = "<h2>A2A panel link opened successfully</h2>"


@app.get("/api/panels")
def panels():
    return {"items": [PANEL]}


@app.get("/api/panels/{panel_id}/render")
def render(panel_id: str):
    return HTMLResponse(wrap_panel_document(MARKUP, PANEL["title"]))


@app.get("/api/panels/{panel_id}")
def detail(panel_id: str):
    return {**PANEL, "html": MARKUP}


@app.get("/api/panels/{panel_id}/versions")
def versions(panel_id: str):
    return {"items": []}


@app.get("/threads")
def threads():
    return []
