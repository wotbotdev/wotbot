"""Fresh panel-generation evaluation against a running WoTBot stack.

Uses the real creation tool, browser service, visual reviewer and WoT runtime.
Only source data and an HTTP sensor are fixtures. No real devices are changed.
Run inside the backend container, with --fixture-host set to its network name.
Evidence is exported before temporary database resources are removed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from sqlalchemy import delete

from wotbot.agent.tools.create_web_interface import create_web_interface
from wotbot.catalog import validate_document
from wotbot.catalog.service import ThingCatalogWriteService
from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.database import get_session_factory
from wotbot.core.llm import make_llm
from wotbot.core.settings import Settings
from wotbot.core.time import utc_now
from wotbot.panels.models import PanelData, PanelValidationReport
from wotbot.panels.visual_review import model_usage
from wotbot.threads.store import ThreadStore

CASES = [
    ("map", "A Leaflet map with OpenStreetMap tiles, three site markers and a compact legend."),
    ("chart", "A responsive SVG bar chart of site values, with readable labels and a summary."),
    ("live", "A live temperature card with unit, timestamp and an accessible refresh button."),
    ("map", "A Leaflet map beside a ranked site table; stack the table below on narrow screens."),
    ("chart", "A line chart of the six monthly values, including units, month labels and min/max."),
    ("live", "A live temperature gauge with a clear numeric reading and normal-range explanation."),
    ("map", "A Leaflet map with circle markers scaled by site value and a summary below it."),
    ("chart", "A horizontal bar ranking of the sites, plus a compact monthly data table."),
    (
        "live",
        "A live temperature panel with current reading and a comparison to attached target 22 C.",
    ),
    ("map", "A Leaflet overview map with numbered markers and a matching site list."),
]
DATA = {
    "sites": [
        {"name": "North ridge", "lat": 49.57, "lon": 7.08, "value": 42},
        {"name": "Lake shore", "lat": 49.55, "lon": 7.06, "value": 31},
        {"name": "Village west", "lat": 49.53, "lon": 7.10, "value": 24},
    ],
    "monthly": [
        {"month": month, "value": value}
        for month, value in zip(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun"], [12, 18, 15, 28, 35, 32], strict=True
        )
    ],
    "unit": "MWh",
    "target": 22,
}


class SensorHandler(BaseHTTPRequestHandler):
    reads = 0

    def do_GET(self):
        if self.path != "/reading":
            self.send_error(404)
            return
        type(self).reads += 1
        body = json.dumps({"value": 21.5, "unit": "C", "timestamp": utc_now().isoformat()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


async def evaluate(args):
    settings = Settings()
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(("0.0.0.0", 0), SensorHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    thing_id = "urn:panel-eval:" + uuid4().hex
    data_id = "panel-data-" + uuid4().hex
    registered = False
    threads = []
    records = []
    document = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": "Temporary panel evaluation sensor",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "reading": {
                "type": "object",
                "readOnly": True,
                "properties": {
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "timestamp": {"type": "string"},
                },
                "forms": [
                    {
                        "href": f"http://{args.fixture_host}:{server.server_port}/reading",
                        "contentType": "application/json",
                        "op": "readproperty",
                    }
                ],
            }
        },
    }
    try:
        with get_session_factory()() as session:
            ThingCatalogWriteService(session).create(validate_document(document))
            registered = True
            raw = json.dumps(DATA)
            session.add(
                PanelData(
                    id=data_id,
                    artifact_id="panel-evaluation",
                    filename="evaluation.json",
                    mime_type="application/json",
                    sha256=hashlib.sha256(raw.encode()).hexdigest(),
                    size_bytes=len(raw.encode()),
                    content=raw,
                    expires_at=utc_now() + timedelta(hours=1),
                )
            )
            session.commit()
        probe = await WotRuntimeClient(settings).read_property(
            thing_id=thing_id, property_name="reading"
        )
        if not probe.get("result", {}).get("success") or not SensorHandler.reads:
            raise RuntimeError("The WoT runtime could not read the temporary HTTP sensor")
        llm = make_llm(settings, timeout=120, max_retries=0, max_tokens=12000).bind_tools(
            # Match agent/nodes.py: strict mode cannot express arbitrary data
            # attachment names and some providers default to it when omitted.
            [create_web_interface],
            strict=False,
            tool_choice="create_web_interface",
            parallel_tool_calls=False,
        )
        for index, (shape, prompt) in enumerate(CASES[: args.runs], start=1):
            folder = root / f"{index:02d}-{shape}"
            folder.mkdir()
            thread = ThreadStore().create(title=f"Panel evaluation {index}: {shape}", visible=False)
            threads.append(thread["id"])
            builder = StateGraph(MessagesState)
            builder.add_node("tools", ToolNode([create_web_interface]))
            builder.add_edge(START, "tools")
            builder.add_edge("tools", END)
            graph = builder.compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": thread["id"]}}
            request = (
                f"Create one panel: {prompt}\nUse data attachment name 'sample', reference {data_id}. "
                f"Its exact fixture content is {json.dumps(DATA)}. Read it via panelData, never copy coordinates. "
                "Design for 1000px and 390px widths. Use the tool's browser checks. "
                "Self-checks should assert observable rendering and attached values; avoid unconditional assertions. "
            )
            if shape == "live":
                request += (
                    f"Read live property 'reading' of Thing {thing_id} through wot.readProperty. "
                    "It returns {value:number, unit:string, timestamp:string} directly. "
                    "Declare only that readProperty capability. No writes, actions, subscriptions or polling. "
                )
            else:
                request += "Do not declare Thing capabilities or use live calls. "
            messages = [
                SystemMessage(
                    content="Create the requested panel using create_web_interface. "
                    "Follow its authoring contract. If validation fails and retry_allowed is true, repair it. "
                    "Do not retry an inconclusive result or advisory visual warning."
                ),
                HumanMessage(content=request),
            ]
            write_json(folder / "request.json", {"shape": shape, "prompt": request})
            start = time.monotonic()
            record = {"case": index, "shape": shape, "attempts": [], "human_visual_review": None}
            for attempt in range(1, 4):
                generation_start = time.monotonic()
                answer = await llm.ainvoke(messages)
                generation_ms = round((time.monotonic() - generation_start) * 1000)
                if (
                    len(answer.tool_calls) != 1
                    or answer.tool_calls[0]["name"] != "create_web_interface"
                ):
                    record["error"] = "Model did not produce exactly one panel tool call"
                    break
                call = answer.tool_calls[0]
                (folder / f"attempt-{attempt}.html").write_text(call["args"].get("html", ""))
                write_json(folder / f"attempt-{attempt}-args.json", call["args"])
                tool_start = time.monotonic()
                state = await graph.ainvoke(
                    {"messages": messages + [answer] if attempt == 1 else [answer]}, config
                )
                try:
                    result = json.loads(state["messages"][-1].content)
                except json.JSONDecodeError:
                    result = {
                        "error": state["messages"][-1].content,
                        "browser_validation": {
                            "status": "failed",
                            "retry_allowed": attempt < 3,
                            "diagnostics": [
                                {"kind": "arguments", "message": "Invalid tool arguments"}
                            ],
                        },
                    }
                report = result.get("browser_validation", {})
                sample = {
                    "attempt": attempt,
                    "generation_ms": generation_ms,
                    "tool_ms": round((time.monotonic() - tool_start) * 1000),
                    "generation_usage": model_usage(answer),
                    "report": report,
                }
                record["attempts"].append(sample)
                report_id = report.get("report_id")
                if report_id:
                    with get_session_factory()() as session:
                        row = session.get(PanelValidationReport, report_id)
                        for viewport, png in [
                            ("normal", row.screenshot),
                            ("narrow", row.narrow_screenshot),
                        ]:
                            if png:
                                (folder / f"attempt-{attempt}-{viewport}.png").write_bytes(png)
                write_json(folder / f"attempt-{attempt}-result.json", result)
                messages = state["messages"]
                if result.get("artifacts") or not report.get("retry_allowed"):
                    record["delivered"] = bool(result.get("artifacts"))
                    break
            record["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            record["first_attempt_passed"] = bool(
                record["attempts"] and record["attempts"][0]["report"].get("status") == "passed"
            )
            record["repairs"] = max(0, len(record["attempts"]) - 1)
            records.append(record)
            write_json(folder / "metrics.json", record)
            write_json(root / "results.json", {"model": settings.openai_model, "cases": records})
            last = record["attempts"][-1]["report"] if record["attempts"] else {}
            print(
                json.dumps(
                    {
                        "case": index,
                        "shape": shape,
                        "delivered": record.get("delivered", False),
                        "attempts": len(record["attempts"]),
                        "visual_status": (last.get("visual_review") or {}).get("status"),
                        "elapsed_ms": record["elapsed_ms"],
                    }
                ),
                flush=True,
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        for thread_id in threads:
            ThreadStore().delete(thread_id)
        with get_session_factory()() as session:
            session.execute(delete(PanelData).where(PanelData.id == data_id))
            session.commit()
            if registered:
                ThingCatalogWriteService(session).delete(thing_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for evidence (must not exist)"
    )
    parser.add_argument(
        "--fixture-host",
        default="127.0.0.1",
        help="Hostname at which the runtime can reach this process",
    )
    parser.add_argument("--runs", type=int, default=10, choices=range(1, 11))
    asyncio.run(evaluate(parser.parse_args()))
