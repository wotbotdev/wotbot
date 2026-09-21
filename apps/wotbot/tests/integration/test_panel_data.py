"""Real database coverage for data-only panels and their retained snapshots."""

import asyncio
import hashlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from wotbot.auth import User, get_current_user
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.panels.data import resolve_data
from wotbot.panels.models import PanelData
from wotbot.panels.router import router
from wotbot.panels.service import PanelService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def environment(jobs_integration_environment):
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE panels, panel_data CASCADE"))
        session.commit()


def snapshot(value):
    content = json.dumps(value, ensure_ascii=False, allow_nan=False)
    raw = content.encode()
    client = AsyncMock()
    client.artifact_metadata.return_value = {
        "mime_type": "application/geo+json",
        "filename": "areas.geojson",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
    }
    client.read_artifact.return_value = raw
    refs, contents, metadata = asyncio.run(resolve_data({"areas": "file-analysis.geojson"}, client))
    assert contents == {"areas": content}
    assert "coordinates" not in json.dumps(metadata)
    return refs, content


def test_snapshots_survive_source_expiry_edits_restore_and_shared_panel_deletion():
    original = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Exact"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [7.12345678912345, 49.5],
                            [7.13, 49.5],
                            [7.13, 49.51],
                            [7.12345678912345, 49.5],
                        ]
                    ],
                },
            }
        ]
        * 150,
    }
    refs, content = snapshot(original)
    other_refs, other_content = snapshot({"type": "FeatureCollection", "features": []})
    with get_session_factory()() as session:
        service = PanelService(session)
        panel = service.create_panel(
            title="Map",
            html='<script>panelData.read("areas")</script>',
            capabilities=[],
            source_thread_id="analysis",
            data=refs,
        )
        original_version = service.list_versions(panel["id"])["items"][0]["id"]
        shared = service.create_panel(
            title="Shared",
            html="<div>Shared</div>",
            capabilities=[],
            source_thread_id=None,
            data=refs,
        )
        assert session.get(PanelData, refs["areas"]).expires_at is None
        assert service.get_render_payload(panel["id"])[2] == {"areas": content}

        # Metadata/source editing must never send the payload to the model/UI editor.
        detail = service.get_panel(panel["id"], include_html=True)
        assert detail["data"] == refs
        assert "7.123456789" not in json.dumps(detail)
        service.update_panel(panel["id"], title="Renamed", html="<div>Updated</div>")
        assert service.get_render_payload(panel["id"])[2]["areas"] == content
        service.update_panel(panel["id"], data=other_refs)
        assert service.get_render_payload(panel["id"])[2]["areas"] == other_content
        service.restore_version(panel["id"], original_version)
        assert service.get_render_payload(panel["id"])[2]["areas"] == content

    # Reusing a retained snapshot never accesses an expired/deleted executor file.
    offline = AsyncMock()
    offline.artifact_metadata.side_effect = AssertionError("Source export has expired")
    resolved, contents, _ = asyncio.run(resolve_data(refs, offline))
    assert resolved == refs and contents == {"areas": content}
    offline.read_artifact.assert_not_called()
    with get_session_factory()() as session:
        service = PanelService(session)
        service.delete_panel(panel["id"])
        assert service.get_render_payload(shared["id"])[2]["areas"] == content
        assert session.get(PanelData, other_refs["areas"]) is None
        service.delete_panel(shared["id"])
        session.expire_all()
        assert session.get(PanelData, refs["areas"]) is None


def test_expired_unpinned_snapshot_is_rejected_and_pruned():
    refs, _ = snapshot({"values": [1, 2, 3]})
    with get_session_factory()() as session:
        session.get(PanelData, refs["areas"]).expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    with pytest.raises(ValueError, match="expired"):
        asyncio.run(resolve_data(refs, AsyncMock()))
    snapshot({"values": [4, 5]})
    with get_session_factory()() as session:
        assert session.get(PanelData, refs["areas"]) is None


def test_migration_refuses_to_discard_retained_panel_data():
    from importlib.resources import files

    refs, content = snapshot({"values": [1, 2, 3]})
    with get_session_factory()() as session:
        panel = PanelService(session).create_panel(
            title="Keep", html="<div>Keep</div>", capabilities=[], source_thread_id=None, data=refs
        )
    with pytest.raises(RuntimeError, match="attachments remain"):
        command.downgrade(Config(str(files("wotbot") / "alembic.ini")), "0008_agent_execution")
    with get_session_factory()() as session:
        assert PanelService(session).get_render_payload(panel["id"])[2]["areas"] == content


def test_panel_routes_pin_and_render_attached_data_without_returning_payload_in_source():
    refs, content = snapshot(
        {"label": "</script><script>window.injected=true</script>", "values": list(range(1500))}
    )
    app = FastAPI()
    app.include_router(router)
    # Exercise the actual guards and handlers with an authenticated operator.
    app.dependency_overrides[get_current_user] = lambda: User(
        user_id="panel-test", scopes=["things:read", "things:write"]
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/panels",
            json={"title": "Map", "html": "<div>Map</div>", "capabilities": [], "data": refs},
        )
        assert response.status_code == 200, response.text
        panel = response.json()
        rendered = client.get(f"/api/panels/{panel['id']}/render")
        assert rendered.status_code == 200
        assert "panelData" in rendered.text and "1499" in rendered.text
        assert "</script><script>window.injected" not in rendered.text
        source = client.get(f"/api/panels/{panel['id']}?include_html=true").json()
        assert source["data"] == refs and content not in json.dumps(source)
        assert (
            client.patch(f"/api/panels/{panel['id']}", json={"title": "New title"}).status_code
            == 200
        )
        assert "1499" in client.get(f"/api/panels/{panel['id']}/render").text
