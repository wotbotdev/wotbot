import asyncio
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from code_executor.models import Settings
from code_executor.session_pool import SessionPool, _SessionEntry


class SessionConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.pool = SessionPool(Settings(_env_file=None, artifacts_dir=root))
        self.entry = _SessionEntry(worker_pid=11, parent_conn=Mock())
        self.pool._sessions["shared"] = self.entry

    async def test_request_during_worker_handoff_keeps_the_same_session(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def communicate(entry, code, execution_id):
            if code == "first":
                started.set()
                await release.wait()
            else:
                self.assertEqual(entry.worker_pid, 22)
            return {"ok": True, "stdout": code, "worker_pid": 22}

        with (
            patch.object(self.pool, "_communicate", side_effect=communicate),
            patch(
                "code_executor.session_pool.pid_is_alive",
                side_effect=lambda pid: pid == 22 or not started.is_set(),
            ),
            patch("code_executor.session_pool.mp.Pipe", return_value=(Mock(), Mock())),
            patch(
                "code_executor.session_pool.mp.Process",
                side_effect=AssertionError(
                    "The promoted worker is still handling this session"
                ),
            ),
        ):
            first = asyncio.create_task(self.pool.execute("shared", "first"))
            await started.wait()
            second = asyncio.create_task(self.pool.execute("shared", "second"))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertEqual([result["stdout"] for result in results], ["first", "second"])
        self.entry.parent_conn.close.assert_not_called()

    async def test_repeated_cancellation_drains_result_before_admitting_next_run(self):
        started, release = asyncio.Event(), asyncio.Event()
        communication_cancelled = []

        async def communicate(entry, code, execution_id):
            if code == "first":
                file = self.pool._file_store.save(
                    execution_id, "partial", filename="data.txt"
                )
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    communication_cancelled.append(True)
                    raise
                return {"ok": True, "files": [file], "worker_pid": 22}
            self.assertTrue(release.is_set(), "The previous exchange must finish first")
            self.assertEqual(entry.worker_pid, 22)
            return {"ok": True, "stdout": "second"}

        with (
            patch.object(self.pool, "_communicate", side_effect=communicate),
            patch("code_executor.session_pool.pid_is_alive", return_value=True),
        ):
            first = asyncio.create_task(self.pool.execute("shared", "first"))
            await started.wait()
            first.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            second = asyncio.create_task(self.pool.execute("shared", "second"))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(results[1]["stdout"], "second")
        self.assertEqual(communication_cancelled, [])
        self.assertEqual(list((self.pool._file_store.root / ".files").iterdir()), [])
        self.assertEqual(list((self.pool._file_store.root / ".staging").iterdir()), [])
