import asyncio
import base64
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from wotbot.auth import User, get_current_user
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.panels.models import PanelValidationReport
from wotbot.panels.reports import save_report
from wotbot.panels.router import router
from wotbot.threads.models import Thread, ThreadKind
from wotbot.threads.store import ThreadStore

pytestmark = pytest.mark.integration
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aE1sAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def environment(jobs_integration_environment):
    with get_session_factory()() as session:
        session.execute(delete(PanelValidationReport))
        session.commit()


def save(thread_id=None, status="failed", previous=None, screenshot=True):
    return asyncio.run(
        save_report(
            title="Map",
            document="<h1>Map</h1>",
            thread_id=thread_id,
            report={
                "status": status,
                "previous_reports": previous or [],
                "diagnostics": [{"kind": "javascript", "message": "Map failed"}]
                if status == "failed"
                else [],
            },
            screenshot_base64=base64.b64encode(PNG).decode() if screenshot else None,
            narrow_screenshot_base64=base64.b64encode(PNG).decode() if screenshot else None,
        )
    )


def client(scopes=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: User(
        user_id="test", scopes=scopes if scopes is not None else ["things:read"]
    )
    return TestClient(app)


def test_saved_failure_and_success_history_survive_new_sessions_and_serve_png():
    thread = ThreadStore().create(thread_id="report-chat")
    first = save(thread["id"])
    last = save(thread["id"], "passed", [first["report_id"]])
    with client() as http:
        report = http.get(f"/api/panel-validation/{last['report_id']}")
        assert report.status_code == 200
        assert report.headers["cache-control"] == "private, no-store"
        assert report.json()["previous_reports"] == [first["report_id"]]
        assert "screenshot_base64" not in report.text and "<h1>" not in report.text
        image = http.get(f"/api/panel-validation/{first['report_id']}/screenshot")
        assert image.status_code == 200 and image.content == PNG
        assert image.headers["content-type"] == "image/png"
        assert image.headers["cache-control"] == "private, no-store"
        assert report.json()["has_narrow_screenshot"] is True
        narrow = http.get(f"/api/panel-validation/{first['report_id']}/screenshot?viewport=narrow")
        assert narrow.status_code == 200 and narrow.content == PNG
        assert (
            http.get(
                f"/api/panel-validation/{first['report_id']}/screenshot?viewport=bad"
            ).status_code
            == 422
        )
    with get_session_factory()() as session:
        session.execute(delete(Thread).where(Thread.id == thread["id"]))
        session.commit()
    with client() as http:
        assert http.get(f"/api/panel-validation/{first['report_id']}").status_code == 404


def test_expired_evidence_is_inaccessible_and_pruned_on_next_save():
    report = save()
    with get_session_factory()() as session:
        session.get(PanelValidationReport, report["report_id"]).expires_at = utc_now() - timedelta(
            seconds=1
        )
        session.commit()
    with client() as http:
        assert http.get(f"/api/panel-validation/{report['report_id']}").status_code == 404
        assert (
            http.get(f"/api/panel-validation/{report['report_id']}/screenshot").status_code == 404
        )
    save()
    with get_session_factory()() as session:
        assert session.get(PanelValidationReport, report["report_id"]) is None


def test_report_requires_read_scope_and_missing_screenshots_are_explicit():
    report = save(screenshot=False)
    with client(scopes=["things:write"]) as http:
        assert http.get(f"/api/panel-validation/{report['report_id']}").status_code == 403
    with client() as http:
        assert http.get(f"/api/panel-validation/{report['report_id']}").status_code == 200
        assert (
            http.get(f"/api/panel-validation/{report['report_id']}/screenshot").status_code == 404
        )


@pytest.mark.parametrize("kind", [ThreadKind.A2A, ThreadKind.MCP_RAW])
def test_external_conversation_evidence_cannot_leak_through_ui_routes(kind):
    thread = ThreadStore().create(kind=kind)
    report = save(thread["id"])
    with client() as http:
        assert http.get(f"/api/panel-validation/{report['report_id']}").status_code == 404
        assert (
            http.get(f"/api/panel-validation/{report['report_id']}/screenshot").status_code == 404
        )


def test_invalid_or_oversized_evidence_is_rejected():
    for screenshot in (
        "not base64",
        base64.b64encode(b"not a PNG").decode(),
        "A" * (6 * 1024 * 1024),
    ):
        with pytest.raises(ValueError):
            asyncio.run(
                save_report(
                    title="Map",
                    document="",
                    thread_id=None,
                    report={},
                    screenshot_base64=screenshot,
                )
            )
