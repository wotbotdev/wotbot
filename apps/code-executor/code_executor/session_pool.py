"""Manages a pool of isolated Python processes, one per chat session."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from code_executor.constants import SESSION_WATCHDOG_GRACE_SECONDS
from code_executor.file_artifacts import FileArtifacts
from code_executor.models import Settings
from code_executor.processes import pid_is_alive, terminate_pid
from code_executor.worker import worker_loop

logger = logging.getLogger(__name__)


@dataclass
class _SessionEntry:
    worker_pid: int
    parent_conn: mp.connection.Connection
    last_used: float = field(default_factory=time.time)
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionPool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._artifacts_dir = settings.artifacts_dir
        self._file_store = FileArtifacts(settings)

    def _get_or_create_session(self, session_id: str) -> _SessionEntry:
        if session_id in self._sessions:
            return self._sessions[session_id]

        if len(self._sessions) >= self._settings.max_sessions:
            raise RuntimeError(
                f"Maximum number of sessions ({self._settings.max_sessions}) reached"
            )

        logger.info("Creating isolated process session for %s", session_id)

        parent_conn, child_conn = mp.Pipe()
        process = mp.Process(
            target=worker_loop,
            args=(
                child_conn,
                self._artifacts_dir,
                self._settings.wot_runtime_url,
                self._settings.wot_runtime_api_token,
                self._settings.execution_timeout_seconds,
                self._settings.model_dump(
                    include={
                        "artifacts_ttl_seconds",
                        "file_artifacts_ttl_seconds",
                        "artifact_max_bytes",
                        "artifact_max_files_per_execution",
                        "artifact_max_execution_bytes",
                    }
                ),
            ),
            daemon=True,
        )
        process.start()
        child_conn.close()

        entry = _SessionEntry(worker_pid=process.pid or -1, parent_conn=parent_conn)
        self._sessions[session_id] = entry
        return entry

    def _worker_is_available(self, session_id: str, entry: _SessionEntry) -> bool:
        if pid_is_alive(entry.worker_pid):
            return True

        logger.warning("Worker process for session %s is dead, recreating", session_id)
        try:
            entry.parent_conn.close()
        except Exception:
            pass
        del self._sessions[session_id]
        return False

    async def execute(self, session_id: str, code: str) -> dict[str, Any]:
        """Execute code in the isolated session process, return artifacts and stdout."""
        while True:
            async with self._lock:
                entry = self._get_or_create_session(session_id)
            async with entry.execution_lock:
                async with self._lock:
                    # Check liveness only after the preceding execution has
                    # reported its promoted worker PID and released this lock.
                    if self._sessions.get(session_id) is not entry:
                        continue
                    if not self._worker_is_available(session_id, entry):
                        continue
                return await self._execute_entry(entry, code)

    async def _execute_entry(self, entry: _SessionEntry, code: str) -> dict[str, Any]:
        entry.last_used = time.time()
        execution_id = uuid4().hex
        communication = asyncio.create_task(
            self._communicate(entry, code, execution_id)
        )
        try:
            cancellation = None
            while True:
                try:
                    result = await asyncio.shield(communication)
                    break
                except asyncio.CancelledError as exc:
                    if communication.cancelled():
                        raise
                    # Repeated cancellation must also leave the pipe exchange
                    # running until it can be drained under the session lock.
                    cancellation = exc
                except Exception:
                    if cancellation is not None:
                        raise cancellation
                    raise
            next_worker_pid = result.pop("worker_pid", None)
            if next_worker_pid:
                entry.worker_pid = next_worker_pid
            if cancellation is not None:
                raise cancellation
            if result.get("ok") is True:
                result["files"] = self._file_store.publish(
                    execution_id, result.get("files", [])
                )
        finally:
            self._file_store.discard(execution_id)
            entry.last_used = time.time()

        if not isinstance(result.get("ok"), bool):
            raise RuntimeError(
                result.get("error") or "Code execution returned no status"
            )

        return result

    async def _communicate(
        self,
        entry: _SessionEntry,
        code: str,
        execution_id: str,
    ) -> dict[str, Any]:
        timeout = self._settings.execution_timeout_seconds
        watchdog_timeout = timeout + SESSION_WATCHDOG_GRACE_SECONDS
        loop = asyncio.get_event_loop()

        def _send_and_receive() -> dict[str, Any]:
            entry.parent_conn.send((code, execution_id))
            if not entry.parent_conn.poll(watchdog_timeout):
                raise TimeoutError(
                    "Code execution session became unresponsive while waiting "
                    "for a result"
                )
            return entry.parent_conn.recv()

        try:
            return await loop.run_in_executor(None, _send_and_receive)
        except TimeoutError:
            await self.shutdown_by_entry(entry)
            raise RuntimeError(
                "Code execution session became unresponsive after "
                f"{watchdog_timeout} seconds."
            ) from None
        except (BrokenPipeError, EOFError, OSError):
            await self.shutdown_by_entry(entry)
            raise RuntimeError(
                "Code execution session crashed. Please try again."
            ) from None

    async def shutdown(self, session_id: str) -> None:
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry:
                logger.info("Removed session %s", session_id)
                self._shutdown_entry(entry)

    async def shutdown_by_entry(self, entry: _SessionEntry) -> None:
        async with self._lock:
            for session_id, session_entry in list(self._sessions.items()):
                if session_entry is entry:
                    del self._sessions[session_id]
                    logger.info("Removed session %s", session_id)
                    self._shutdown_entry(entry)
                    break

    def _shutdown_entry(self, entry: _SessionEntry) -> None:
        try:
            entry.parent_conn.send(None)
        except Exception:
            pass
        finally:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not pid_is_alive(entry.worker_pid):
                    break
                time.sleep(0.05)
            terminate_pid(entry.worker_pid)
            try:
                entry.parent_conn.close()
            except Exception:
                pass

    async def cleanup_idle(self) -> None:
        now = time.time()
        async with self._lock:
            to_remove = [
                sid
                for sid, entry in self._sessions.items()
                if not entry.execution_lock.locked()
                and now - entry.last_used > self._settings.idle_timeout_seconds
            ]
        for sid in to_remove:
            logger.info("Reaping idle session %s", sid)
            await self.shutdown(sid)

    def cleanup_old_artifacts(self) -> None:
        self._file_store.cleanup()
        now = time.time()
        ttl = self._settings.artifacts_ttl_seconds
        try:
            for f in os.listdir(self._artifacts_dir):
                filepath = os.path.join(self._artifacts_dir, f)
                if os.path.isfile(filepath):
                    age = now - os.path.getmtime(filepath)
                    if age > ttl:
                        os.remove(filepath)
        except OSError:
            pass

    async def shutdown_all(self) -> None:
        async with self._lock:
            session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.shutdown(sid)

    @property
    def active_count(self) -> int:
        return len(self._sessions)
