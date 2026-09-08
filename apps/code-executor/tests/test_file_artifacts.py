import hashlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from code_executor.api.app import app
from code_executor.file_artifacts import FileArtifacts
from code_executor.models.settings import Settings
from code_executor.session_pool import SessionPool


class FileArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(
            _env_file=None,
            artifacts_dir=str(self.root),
            artifact_max_bytes=64,
            artifact_max_execution_bytes=80,
            artifact_max_files_per_execution=2,
            artifacts_ttl_seconds=120,
            file_artifacts_ttl_seconds=120,
        )
        self.store = FileArtifacts(self.settings)
        self.run = uuid4().hex

    def test_original_content_is_hidden_until_publication_and_http_preserves_bytes(
        self,
    ):
        self.enterContext(
            patch.dict(
                os.environ,
                {"ARTIFACTS_DIR": str(self.root), "INTERNAL_API_KEY": "test"},
            )
        )
        client = self.enterContext(TestClient(app))
        for name, data, mime in [
            ("data.json", b'{ "b": 1, "a": 2 }\r\n', "application/json"),
            ("data.csv", "name\r\nMüller\r\n".encode(), "text/csv"),
            ("binary.bin", b"\xff\x00\xfe", "application/octet-stream"),
            ("empty.csv", b"", "text/csv"),
        ]:
            with self.subTest(name=name):
                run = uuid4().hex
                staged = self.store.save(
                    run, io.BytesIO(data), filename=name, mime_type=mime
                )
                with self.assertRaises(FileNotFoundError):
                    self.store.describe(staged["id"])
                descriptor = self.store.publish(run, [staged])[0]
                self.store.discard(run)
                self.assertEqual(descriptor["sha256"], hashlib.sha256(data).hexdigest())
                response = client.get(
                    f"/artifacts/{staged['id']}/content",
                    headers={"Authorization": "Bearer test"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, data)
                self.assertEqual(
                    response.headers["x-artifact-sha256"], descriptor["sha256"]
                )
                self.assertIn(name, response.headers["content-disposition"])
                self.assertEqual(
                    client.get(f"/artifacts/{staged['id']}/content").status_code, 401
                )

    def test_limits_streaming_and_failed_save_cleanup(self):
        self.store.save(self.run, b"a" * 64, filename="a.csv")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.store.save(self.run, io.BytesIO(b"b" * 17), filename="b.csv")
        self.store.save(self.run, b"b" * 16, filename="b.manifest")
        with self.assertRaisesRegex(ValueError, "count limit"):
            self.store.save(self.run, b"", filename="c.csv")
        self.assertEqual(len(list(self.store.stage_dir(self.run).iterdir())), 4)

    def test_expiry_reaps_file_and_manifest_without_read_renewal(self):
        staged = self.store.save(self.run, "hello", filename="hello.txt")
        self.store.publish(self.run, [staged])
        path = self.store.path(staged["id"])
        modified = time.time() - 121
        os.utime(path, (modified, modified))
        with self.assertRaises(FileNotFoundError):
            self.store.describe(staged["id"])
        self.assertEqual(path.stat().st_mtime, modified)
        self.store.cleanup()
        self.assertFalse((self.root / ".manifests" / staged["id"]).exists())
        with self.assertRaises(FileNotFoundError):
            self.store.describe(staged["id"])

    def test_retention_starts_when_execution_publishes_the_file(self):
        staged = self.store.save(self.run, "hello", filename="hello.txt")
        path = self.store.stage_dir(self.run) / staged["id"]
        os.utime(path, (time.time() - 121,) * 2)
        published_at = time.time()
        descriptor = self.store.publish(self.run, [staged])[0]
        self.assertGreaterEqual(
            self.store.path(staged["id"]).stat().st_mtime, published_at
        )
        self.assertEqual(descriptor["ttl_seconds"], 120)

    def test_published_files_survive_reopened_store_and_legacy_cleanup(self):
        settings = self.settings.model_copy(
            update={"file_artifacts_ttl_seconds": 604800}
        )
        store = FileArtifacts(settings)
        staged = store.save(self.run, "persist", filename="persist.csv")
        original = store.publish(self.run, [staged])[0]
        file = store.path(staged["id"])
        modified = time.time() - 3601
        os.utime(file, (modified, modified))
        (self.root / "old.png").write_bytes(b"preview")
        os.utime(self.root / "old.png", (modified, modified))
        reopened = FileArtifacts(settings)
        descriptor = reopened.describe(staged["id"])
        self.assertEqual(descriptor["ttl_seconds"], 604800)
        SessionPool(settings).cleanup_old_artifacts()
        self.assertEqual(reopened.path(staged["id"]).read_bytes(), b"persist")
        self.assertEqual(descriptor["sha256"], original["sha256"])
        self.assertEqual(file.stat().st_mtime, modified)
        self.assertFalse((self.root / "old.png").exists())
        os.utime(file, (time.time() - 604801,) * 2)
        with self.assertRaises(FileNotFoundError):
            reopened.path(staged["id"])
        reopened.cleanup()
        self.assertFalse(file.exists())
        self.assertFalse((self.root / ".manifests" / staged["id"]).exists())

    def test_rejects_paths_and_symlinks(self):
        for name in ["../private", ".manifests", "a/b", "a\\b", "a..b"]:
            with self.assertRaises(ValueError):
                self.store.path(name)
        (self.root / "link.csv").symlink_to("/etc/hosts")
        with self.assertRaises(FileNotFoundError):
            self.store.path("link.csv")
        with self.assertRaises(TypeError):
            self.store.save(self.run, {"a": 1}, filename="a.json")
        with self.assertRaises(TypeError):
            self.store.save(self.run, io.StringIO("text"), filename="a.txt")


class FileExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_failure_timeout_and_worker_death(self):
        # Warm imports/font caches before the short execution watchdog starts.
        from code_executor.execution_environment import ExecutionEnvironment

        with tempfile.TemporaryDirectory() as warm:
            ExecutionEnvironment(warm, "http://unused", "")
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(
                _env_file=None, artifacts_dir=root, execution_timeout_seconds=2
            )
            pool = SessionPool(settings)
            try:
                first = await pool.execute(
                    "files", "save_artifact('x,y\\r\\n1,2\\r\\n', filename='data.csv')"
                )
                self.assertTrue(first["ok"])
                self.assertEqual(len(first["files"]), 1)
                self.assertEqual(first["stdout"], "")
                identity = first["files"][0]["id"]
                for code, lost_result in [
                    (
                        "save_artifact('failed', filename='fail.txt'); raise ValueError('failed')",
                        False,
                    ),
                    (
                        "save_artifact('timeout', filename='timeout.txt'); import time; time.sleep(10)",
                        True,
                    ),
                    (
                        "save_artifact('exit', filename='exit.txt'); import os; os._exit(1)",
                        True,
                    ),
                ]:
                    if lost_result:
                        with self.assertRaises(RuntimeError):
                            await pool.execute("files", code)
                    else:
                        result = await pool.execute("files", code)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error"], "ValueError: failed")
                        self.assertEqual(result["files"], [])
                    self.assertEqual(
                        [p.name for p in (Path(root) / ".files").iterdir()], [identity]
                    )
                    self.assertEqual(list((Path(root) / ".staging").iterdir()), [])
                self.assertEqual(
                    FileArtifacts(settings).path(identity).read_bytes(),
                    b"x,y\r\n1,2\r\n",
                )
            finally:
                await pool.shutdown_all()

    async def test_concurrent_and_cancelled_runs_do_not_publish_partial_files(self):
        import asyncio

        with tempfile.TemporaryDirectory() as root:
            pool = SessionPool(
                Settings(
                    _env_file=None, artifacts_dir=root, execution_timeout_seconds=5
                )
            )
            try:
                first, second = await asyncio.gather(
                    pool.execute(
                        "queue",
                        "import time; time.sleep(0.1); save_artifact('first', filename='same.txt')",
                    ),
                    pool.execute(
                        "queue", "save_artifact('second', filename='same.txt')"
                    ),
                )
                store = FileArtifacts(pool._settings)
                self.assertEqual(
                    store.path(first["files"][0]["id"]).read_bytes(), b"first"
                )
                self.assertEqual(
                    store.path(second["files"][0]["id"]).read_bytes(), b"second"
                )
                cancelled = asyncio.create_task(
                    pool.execute(
                        "queue",
                        "save_artifact('cancelled', filename='cancelled.txt'); import time; time.sleep(0.2)",
                    )
                )
                await asyncio.sleep(0.05)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                self.assertEqual(len(list((Path(root) / ".files").iterdir())), 2)
                self.assertEqual(list((Path(root) / ".staging").iterdir()), [])
                last = await pool.execute(
                    "queue", "save_artifact('last', filename='last.txt')"
                )
                self.assertEqual(
                    store.path(last["files"][0]["id"]).read_bytes(), b"last"
                )
            finally:
                await pool.shutdown_all()
