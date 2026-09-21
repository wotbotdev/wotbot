"""Local acceptance fixtures only; no real credentials or device connections."""

import json
import math
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from wotbot.panels.render import wrap_panel_document

ROOT = Path(__file__).resolve().parent
app = FastAPI()


@app.get("/")
def normal_host():
    return FileResponse(ROOT / "normal-host.html")


@app.get("/normal-panel")
def normal_panel():
    return HTMLResponse(
        wrap_panel_document((ROOT / "panel-fixture.html").read_text(), "Test panel")
    )


@app.get("/blank-panel")
def blank_panel():
    # An empty wrapped document: somewhere to exercise the injected helpers
    # directly, without a fixture panel's own markup also running.
    return HTMLResponse(wrap_panel_document("", "Blank panel"))


@app.get("/data-panel")
def data_panel():
    # Larger than executor stdout, with a hole and full precision coordinates.
    ring = [
        [
            7.12345678912345 + 0.01 * math.cos(i * math.tau / 600),
            49.56789123456789 + 0.006 * math.sin(i * math.tau / 600),
        ]
        for i in range(600)
    ]
    ring.append(ring[0])
    hole = [[7.122, 49.567], [7.124, 49.567], [7.124, 49.568], [7.122, 49.568], [7.122, 49.567]]
    areas = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "area-A",
                "properties": {"name": "Area A", "color": "#276749"},
                "geometry": {"type": "Polygon", "coordinates": [ring, hole]},
            }
        ],
        "label": "</script><script>window.injected=true</script>",
    }
    return HTMLResponse(
        wrap_panel_document(
            (ROOT / "data-panel.html").read_text(),
            "Attached data acceptance",
            data={"areas": json.dumps(areas, allow_nan=False)},
        )
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
