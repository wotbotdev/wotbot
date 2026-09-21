import hashlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from wotbot.core.time import utc_now
from wotbot.panels.data import MAX_DATA_BYTES, parse_json, resolve_data, validate_data_refs
from wotbot.panels.render import wrap_panel_document


@pytest.mark.parametrize(
    "content", ['{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}', "not json"]
)
def test_rejects_json_that_cannot_roundtrip(content):
    with pytest.raises(ValueError):
        parse_json(content)


@pytest.mark.parametrize(
    "refs",
    [
        {"areas": "../secret"},
        {"__proto__": "file-x"},
        {"areas": "http://example.com"},
        {f"a{i}": "file-x" for i in range(9)},
    ],
)
def test_rejects_invalid_attachment_references(refs):
    with pytest.raises(ValueError):
        validate_data_refs(refs)


def test_embedded_data_cannot_break_out_of_script():
    content = json.dumps(
        {
            "label": '</script><script>throw "injected"</script>',
            "geometry": {"coordinates": [[7.12345678912345, 49.3456789123456]]},
        }
    )
    document = wrap_panel_document("<div>Map</div>", data={"areas": content})
    assert "</script><script>throw" not in document
    assert "\\u003c" in document
    assert "panelData" in document
    assert "7.12345678912345" in document


@pytest.mark.anyio
async def test_resolver_never_reads_oversized_artifact():
    client = AsyncMock()
    client.artifact_metadata.return_value = {
        "mime_type": "application/json",
        "size_bytes": MAX_DATA_BYTES + 1,
    }
    with (
        patch("wotbot.panels.data.asyncio.to_thread", return_value=({}, {})),
        pytest.raises(ValueError, match="limit"),
    ):
        await resolve_data({"areas": "file-x.json"}, client, session_factory=object())
    client.read_artifact.assert_not_called()


@pytest.mark.anyio
async def test_resolver_rejects_changed_bytes_and_non_json_mime():
    client = AsyncMock()
    client.artifact_metadata.return_value = {
        "mime_type": "application/json",
        "size_bytes": 2,
        "sha256": "bad",
    }
    client.read_artifact.return_value = b"{}"
    with patch("wotbot.panels.data.asyncio.to_thread", return_value=({}, {})):
        with pytest.raises(ValueError, match="checksum"):
            await resolve_data({"areas": "file-x.json"}, client, session_factory=object())
        client.artifact_metadata.return_value["mime_type"] = "text/html"
        with pytest.raises(ValueError, match="exported as"):
            await resolve_data({"areas": "file-x.json"}, client, session_factory=object())


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["artifact_metadata", "read_artifact"])
@pytest.mark.parametrize("status", [404, 410, 503])
async def test_missing_exports_are_repairable_but_executor_outages_are_not(operation, status):
    client = AsyncMock()
    client.artifact_metadata.return_value = {"mime_type": "application/json", "size_bytes": 2}
    response = httpx.Response(status, request=httpx.Request("GET", "http://executor/artifact"))
    getattr(client, operation).side_effect = httpx.HTTPStatusError(
        "Could not fetch export", request=response.request, response=response
    )
    expected = httpx.HTTPStatusError if status == 503 else ValueError
    with (
        patch("wotbot.panels.data.asyncio.to_thread", return_value=({}, {})),
        pytest.raises(expected) as error,
    ):
        await resolve_data({"areas": "file-missing.json"}, client, session_factory=object())
    if status != 503:
        assert "file-missing.json" in str(error.value)
        assert "missing or expired" in str(error.value)


@pytest.mark.anyio
async def test_resolver_returns_only_refs_metadata_and_separate_original_content():
    content = json.dumps(
        {"features": [{"geometry": {"coordinates": [7.12345678912345, 49.5]}}] * 100}
    )
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
    with patch("wotbot.panels.data.asyncio.to_thread", side_effect=[({}, {}), None]):
        refs, contents, metadata = await resolve_data(
            {"areas": "file-x.geojson"}, client, session_factory=object()
        )
    assert contents == {"areas": content}
    assert refs["areas"].startswith("panel-data-")
    assert metadata["areas"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "coordinates" not in json.dumps(metadata)


@pytest.fixture
def anyio_backend():
    return "asyncio"
