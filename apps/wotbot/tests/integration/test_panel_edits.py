"""Edit responses expose saved validation evidence on success and failure."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import text

from wotbot.auth import User, get_current_user
from wotbot.core.database import get_session_factory
from wotbot.panels.reports import save_report
from wotbot.panels.router import router

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def environment(jobs_integration_environment):
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE panels, panel_data, panel_validation_reports CASCADE"))
        session.commit()


@pytest.mark.parametrize("status", ["passed", "failed", "inconclusive", "unavailable"])
def test_edit_returns_the_matching_report_and_only_applies_successful_markup(status):
    class Graph:
        async def ainvoke(self, state, config):
            self.report = await save_report(
                title="Edited panel",
                document="<h1>Updated</h1>",
                thread_id=config["configurable"]["thread_id"],
                report={
                    "status": status,
                    "diagnostics": []
                    if status == "passed"
                    else [{"kind": "javascript", "message": "Chart failed to load"}],
                    "visual_review": {
                        "status": "warnings",
                        "assessments": [
                            {
                                "viewport": "narrow",
                                "category": "overlap",
                                "verdict": "present",
                                "evidence": "The legend overlaps a label.",
                            }
                        ],
                    },
                },
                screenshot_base64=None,
            )
            result = {"browser_validation": self.report}
            if status == "passed":
                result["artifacts"] = [{"kind": "web", "capabilities": [], "data": {}}]
            else:
                result["error"] = "No panel delivered"
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_web_interface",
                                "id": "edit",
                                "args": {"html": "<h1>Updated</h1>"},
                            }
                        ],
                    ),
                    ToolMessage(tool_call_id="edit", content=json.dumps(result)),
                ]
            }

    graph = Graph()
    app = FastAPI()
    app.include_router(router)
    app.state.graph = graph
    app.state.checkpointer = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: User(
        user_id="editor", scopes=["things:read", "things:write"]
    )
    with TestClient(app) as client:
        panel = client.post(
            "/api/panels", json={"title": "Original", "html": "<h1>Original</h1>"}
        ).json()
        path = f"/api/panels/{panel['id']}"
        response = client.post(f"{path}/edit", json={"instruction": "Update the heading"})
        if status == "passed":
            assert response.status_code == 200
            assert response.json()["id"] == panel["id"]
            report = response.json()["browser_validation"]
        else:
            assert response.status_code == 422
            assert "saved panel was not changed" in response.json()["detail"]["message"]
            report = response.json()["detail"]["browser_validation"]
        assert report == graph.report
        assert "legend overlaps" in report["visual_review"]["assessments"][0]["evidence"]
        saved = client.get(f"/api/panel-validation/{report['report_id']}")
        assert saved.status_code == 200
        assert saved.json()["status"] == status
        if status != "passed":
            assert saved.json()["diagnostics"][0]["message"] == "Chart failed to load"
        detail = client.get(path, params={"include_html": "true"}).json()
        assert detail["html"] == ("<h1>Updated</h1>" if status == "passed" else "<h1>Original</h1>")
        versions = client.get(f"{path}/versions").json()["items"]
        assert len(versions) == (2 if status == "passed" else 1)
    app.state.checkpointer.adelete_thread.assert_awaited_once()
