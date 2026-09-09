"""Successful worker promotion must preserve resources used by its successor."""

import asyncio
import multiprocessing as mp
import os
from unittest.mock import patch

import pytest

from code_executor.models import Settings
from code_executor.session_pool import SessionPool


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Rollback uses POSIX fork")
def test_spawned_worker_handoff_does_not_run_inherited_exit_cleanup(
    tmp_path, monkeypatch
):
    # Uvicorn's reload process uses spawn. Its workers leave through Python's
    # normal exit path, including inherited atexit callbacks, on each promotion.
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    marker = tmp_path / "resource-used-by-live-worker"

    async def exercise():
        pool = SessionPool(
            Settings(_env_file=None, artifacts_dir=str(tmp_path / "artifacts"))
        )
        try:
            result = await pool.execute(
                "handoff",
                "import atexit\nfrom pathlib import Path\nimport time\n"
                f"resource = Path({str(marker)!r})\n"
                "resource.write_text('needed by successor')\n"
                "def cleanup():\n    resource.unlink(missing_ok=True)\n"
                "atexit.register(cleanup)\n"
                "print('registered')",
            )
            assert result["ok"], result
            for _ in range(3):
                result = await pool.execute(
                    "handoff",
                    "time.sleep(0.1)\n"
                    "assert resource.exists(), 'retired worker cleaned live resource'\n"
                    "print('still available')",
                )
                assert result["ok"], result
                assert result["stdout"].strip() == "still available"
        finally:
            await pool.shutdown_all()

    with patch("code_executor.session_pool.mp", mp.get_context("spawn")):
        asyncio.run(exercise())
