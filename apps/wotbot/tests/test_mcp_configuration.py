"""Route mounting is independent for MCP and A2A, including shared downloads."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("a2a,mcp", [(False, False), (True, False), (False, True), (True, True)])
def test_independent_surface_flags(a2a, mcp):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from wotbot.api.main import app; print(json.dumps(sorted({r.path for r in app.routes if hasattr(r, 'path')})))",
        ],
        env={**os.environ, "A2A_ENABLED": str(a2a).lower(), "MCP_ENABLED": str(mcp).lower()},
        capture_output=True,
        text=True,
        check=True,
    )
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert ("/.well-known/agent-card.json" in paths) == a2a
    assert ("/a2a/v1/message:send" in paths) == a2a
    for profile in ("assistant", "intents", "raw"):
        assert ("/mcp/" + profile in paths) == mcp
    assert ("/agent/artifacts/{artifact_id}" in paths) == (a2a or mcp)
    assert ("/a2a/artifacts/{artifact_id}" in paths) == (a2a or mcp)
