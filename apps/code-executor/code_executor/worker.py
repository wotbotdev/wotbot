"""Isolated process worker loop for persistent code execution sessions."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
import traceback
from typing import Any

from code_executor.constants import MAX_STDOUT_CHARS, RESULT_POLL_INTERVAL_SECONDS
from code_executor.execution_environment import ExecutionEnvironment
from code_executor.processes import terminate_pid

_PROMOTED_CURRENT_PROCESS = object()


def worker_loop(
    conn: mp.connection.Connection,
    artifacts_dir: str,
    runtime_url: str,
    runtime_api_token: str,
    execution_timeout_seconds: int,
    file_settings: dict[str, Any] | None = None,
) -> None:
    """The entry point for the isolated background process."""
    environment = ExecutionEnvironment(
        artifacts_dir,
        runtime_url,
        runtime_api_token,
        file_settings,
    )

    while True:
        try:
            message = conn.recv()
            if message is None:
                break
            code, execution_id = message

            if not hasattr(os, "fork"):
                result = environment.execute_code(code, execution_id)
                conn.send(_session_response(result))
                continue

            outcome = _execute_with_rollback(
                environment,
                code,
                execution_timeout_seconds,
                execution_id,
            )
            if outcome is _PROMOTED_CURRENT_PROCESS:
                continue

            response, promoted_child = outcome
            conn.send(response)
            if promoted_child:
                return

        except EOFError:
            break
        except Exception as e:
            conn.send({"error": str(e), "stdout": "", "images": [], "plotly": []})


def _execute_with_rollback(
    environment: ExecutionEnvironment,
    code: str,
    execution_timeout_seconds: int,
    execution_id: str,
) -> tuple[dict[str, Any], bool] | object:
    result_reader, result_writer = mp.Pipe(duplex=False)
    child_pid = os.fork()

    if child_pid == 0:
        result_reader.close()
        result = _run_child_code(environment, code, result_writer, execution_id)
        result_writer.close()

        if result.get("ok"):
            return _PROMOTED_CURRENT_PROCESS
        os._exit(0)

    result_writer.close()
    try:
        return _await_child_result(
            result_reader,
            child_pid,
            execution_timeout_seconds,
        )
    finally:
        result_reader.close()


def _run_child_code(
    environment: ExecutionEnvironment,
    code: str,
    result_writer: mp.connection.Connection,
    execution_id: str | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "stdout": "",
        "images": [],
        "plotly": [],
    }
    try:
        result = environment.execute_code(code, execution_id)
        result_writer.send(result)
    except BaseException as exc:
        result = {
            "ok": False,
            "error": "".join(traceback.format_exception_only(type(exc), exc)).strip()[
                :MAX_STDOUT_CHARS
            ],
            "stdout": environment._trim_stdout(traceback.format_exc()),
            "images": [],
            "plotly": [],
            "wot_calls": environment.wot_calls.copy(),
        }
        try:
            result_writer.send(result)
        except Exception:
            pass
    return result


def _await_child_result(
    result_reader: mp.connection.Connection,
    child_pid: int,
    execution_timeout_seconds: int,
) -> tuple[dict[str, Any], bool]:
    deadline = time.monotonic() + execution_timeout_seconds

    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        poll_timeout = min(RESULT_POLL_INTERVAL_SECONDS, remaining)

        if result_reader.poll(poll_timeout):
            return _receive_child_result(result_reader, child_pid)

        finished_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        if finished_pid == child_pid:
            return _handle_exited_child(result_reader)

    terminate_pid(child_pid)
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass
    return (
        {
            "error": (
                f"Code execution timed out after {execution_timeout_seconds} seconds."
            ),
            "stdout": "",
            "images": [],
            "plotly": [],
        },
        False,
    )


def _receive_child_result(
    result_reader: mp.connection.Connection,
    child_pid: int,
) -> tuple[dict[str, Any], bool]:
    try:
        child_result = result_reader.recv()
    except EOFError:
        return (
            {
                "error": "Code execution worker exited without returning a result.",
                "stdout": "",
                "images": [],
                "plotly": [],
            },
            False,
        )

    if child_result.get("ok"):
        response = _session_response(child_result)
        response["worker_pid"] = child_pid
        return response, True

    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass
    return _session_response(child_result), False


def _handle_exited_child(
    result_reader: mp.connection.Connection,
) -> tuple[dict[str, Any], bool]:
    if result_reader.poll(0):
        try:
            child_result = result_reader.recv()
        except EOFError:
            child_result = None
        if child_result and not child_result.get("ok"):
            return _session_response(child_result), False

    return (
        {
            "error": "Code execution worker exited unexpectedly.",
            "stdout": "",
            "images": [],
            "plotly": [],
        },
        False,
    )


def _session_response(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result["ok"],
        "error": result.get("error"),
        "stdout": result["stdout"],
        "images": result["images"] if result["ok"] else [],
        "plotly": result["plotly"] if result["ok"] else [],
        "files": result.get("files", []) if result["ok"] else [],
        "wot_calls": result.get("wot_calls", []),
        "records": result.get("records", []) if result["ok"] else [],
        "reports": result.get("reports", []) if result["ok"] else [],
    }
