"""Exercise the execution contract through HTTP, the pool and real workers."""

import hashlib
import json
import os
import time
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from code_executor.api.app import app
from code_executor.file_artifacts import FileArtifacts
from code_executor.models import Settings


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="Session rollback requires POSIX fork"
)
def test_failed_run_keeps_status_actions_and_previous_session_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    settings = Settings(
        _env_file=None,
        artifacts_dir=str(tmp_path / "artifacts"),
        internal_api_key="test-execution",
        wot_runtime_url="http://runtime.invalid",
        wot_runtime_api_token="",
        execution_timeout_seconds=20,
    )
    headers = {"Authorization": "Bearer test-execution"}
    with (
        patch("code_executor.api.app.Settings", return_value=settings),
        TestClient(app) as client,
    ):

        def execute(code):
            response = client.post(
                "/execute",
                json={"session_id": "rollback-test", "code": code},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            return response.json()

        initial = execute("state_marker = 1; print('Error: example text')")
        assert initial["ok"] is True
        assert initial["error"] is None

        # Record a simulated completed action without contacting a device.
        failed = execute(
            "state_marker = 2\n"
            "wot._record_call({'type': 'invoke_action', 'thing_id': 'urn:test:device', "
            "'name': 'toggle', 'ok': True})\n"
            "store_record({'value': 2})\n"
            "report('done')\n"
            "plt.plot([1, 2]); plt.show()\n"
            "print('x' * 3000)\n"
            "raise ValueError('after action')\n"
        )
        assert failed["ok"] is False
        assert failed["error"] == "ValueError: after action"
        assert "ValueError" not in failed["stdout"]
        assert failed["wot_calls"][0]["name"] == "toggle"
        for key in ("images", "plotly", "records", "reports"):
            assert failed[key] == []
        assert list((tmp_path / "artifacts").glob("*.png")) == []

        after = execute("print(state_marker)")
        assert after["ok"] is True
        assert after["stdout"].strip() == "1"
        assert after["wot_calls"] == []

        exported = execute(
            "save_artifact('column\\r\\nvalue\\r\\n', filename='data.csv', mime_type='text/csv')"
        )
        assert exported["ok"] is True
        assert exported["stdout"] == ""
        descriptor = exported["files"][0]
        downloaded = client.get(
            f"/artifacts/{descriptor['id']}/content", headers=headers
        )
        assert downloaded.content == b"column\r\nvalue\r\n"
        assert hashlib.sha256(downloaded.content).hexdigest() == descriptor["sha256"]


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="Session rollback requires POSIX fork"
)
def test_analysis_record_raw_input_survives_http_and_invalid_record_rolls_back(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    settings = Settings(
        _env_file=None,
        artifacts_dir=str(tmp_path / "artifacts"),
        internal_api_key="test-records",
        execution_timeout_seconds=20,
    )
    with (
        patch("code_executor.api.app.Settings", return_value=settings),
        TestClient(app) as client,
    ):

        def execute(code):
            response = client.post(
                "/execute",
                json={"session_id": "job-analysis:record-test", "code": code},
                headers={"Authorization": "Bearer test-records"},
            )
            assert response.status_code == 200, response.text
            return response.json()

        result = execute(
            "import json\n"
            "data = {'measurement': {'value': '125', 'unit': 'cm'}}\n"
            "metres = float(data['measurement']['value']) / 100\n"
            "store_record({'metres': metres}, raw_input=data)\n"
            "report('Converted 125 centimetres to 1.25 metres')\n"
            "print(json.dumps({'answer': {'metres': metres}}))"
        )
        assert result["ok"] is True
        assert result["error"] is None
        assert json.loads(result["stdout"]) == {"answer": {"metres": 1.25}}
        assert result["reports"] == ["Converted 125 centimetres to 1.25 metres"]
        assert len(result["records"]) == 1
        assert result["records"][0]["data"] == {"metres": 1.25}
        assert json.loads(result["records"][0]["raw_input"]) == {
            "measurement": {"value": "125", "unit": "cm"}
        }

        failed = execute(
            "metres = 2\n"
            "store_record({'metres': metres})\n"
            "report('partial')\n"
            "store_record({'metres': metres}, confidence='high')"
        )
        assert failed["ok"] is False
        assert "store_record confidence" in failed["error"]
        assert failed["records"] == []
        assert failed["reports"] == []
        after = execute("print(metres)")
        assert after["ok"] is True
        assert after["stdout"].strip() == "1.25"


@pytest.mark.parametrize(
    "filename,content,mime_type",
    [
        ("data.json", b'{"a":1}', "application/json"),
        ("page.html", b"<script>doSomething()</script>", "text/html"),
        ("binary.bin", bytes(range(256)), "application/octet-stream"),
        ("empty.csv", b"", "text/csv"),
    ],
)
def test_file_routes_preserve_bytes_authentication_and_expiry(
    tmp_path, filename, content, mime_type
):
    settings = Settings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        internal_api_key="artifact-test",
        file_artifacts_ttl_seconds=60,
    )
    store = FileArtifacts(settings)
    execution_id = uuid4().hex
    staged = store.save(execution_id, content, filename=filename, mime_type=mime_type)
    descriptor = store.publish(execution_id, [staged])[0]
    store.discard(execution_id)
    artifact_id = descriptor["id"]
    path = store.path(artifact_id)
    original_mtime = path.stat().st_mtime
    headers = {"Authorization": "Bearer artifact-test"}
    with (
        patch("code_executor.api.app.Settings", return_value=settings),
        TestClient(app) as client,
    ):
        for suffix in ("", "/content", "/metadata"):
            assert client.get(f"/artifacts/{artifact_id}{suffix}").status_code == 401
        for suffix in ("", "/content"):
            response = client.get(f"/artifacts/{artifact_id}{suffix}", headers=headers)
            assert response.status_code == 200
            assert response.content == content
            assert response.headers["content-disposition"].startswith("attachment;")
            assert response.headers["x-artifact-sha256"] == descriptor["sha256"]
            assert response.headers["x-artifact-expires-at"] == descriptor["expires_at"]
            assert "no-store" in response.headers["cache-control"]
        metadata = client.get(
            f"/artifacts/{artifact_id}/metadata", headers=headers
        ).json()
        assert metadata["filename"] == filename
        assert metadata["size_bytes"] == len(content)
        assert path.stat().st_mtime == original_mtime
        os.utime(path, (time.time() - 61,) * 2)
        for suffix in ("", "/content", "/metadata"):
            assert (
                client.get(
                    f"/artifacts/{artifact_id}{suffix}", headers=headers
                ).status_code
                == 404
            )
