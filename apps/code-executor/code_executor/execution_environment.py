"""Runtime environment for code executed inside an isolated worker."""

from __future__ import annotations

import ast
import io
import os
import sys
import traceback
import types
import uuid
from contextlib import redirect_stdout
from typing import Any

from code_executor.constants import MAX_STDOUT_CHARS, SENSITIVE_ENV_VARS
from code_executor.file_artifacts import FileArtifacts
from code_executor.models.settings import Settings
from code_executor.wot_client import SandboxWotClient

_LAST_EXPR_GLOBAL = "__code_executor_last_expr__"
_MACHINE_READABLE_STDOUT_PREFIXES = ("__VIRTUAL_THING_RESULT__",)


class ExecutionEnvironment:
    """Owns imports, globals, and captured artifacts for one live worker."""

    def __init__(
        self,
        artifacts_dir: str,
        runtime_url: str,
        runtime_api_token: str,
        file_settings: dict[str, Any] | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.images: list[str] = []
        self.plotly: list[str] = []
        self.files: list[dict[str, Any]] = []
        self.wot_calls: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.reports: list[str] = []

        self._prepare_process_environment()
        self.file_store = FileArtifacts(
            Settings(
                _env_file=None, artifacts_dir=artifacts_dir, **(file_settings or {})
            )
        )
        self._load_runtime_modules()
        self._install_plotly_renderer()

        self.wot = SandboxWotClient(
            runtime_url,
            runtime_api_token,
            self.wot_calls.append,
        )
        self._register_wot_module()
        self.user_globals = self._build_user_globals()

    def execute_code(
        self, code: str, execution_id: str | None = None
    ) -> dict[str, Any]:
        self.execution_id = execution_id or uuid.uuid4().hex
        self.files.clear()
        self.images.clear()
        self.plotly.clear()
        self.wot_calls.clear()
        self.records.clear()
        self.reports.clear()
        stdout_buffer = io.StringIO()

        original_plt_show = self.plt.show
        original_pio_show = self.pio.show
        original_renderer = self.pio.renderers.default

        # Matplotlib's lazy backend initialization sets attributes on plt.show;
        # plain functions accept those attributes, while bound methods do not.
        def capture_matplotlib(*args: Any, **kwargs: Any) -> None:
            self._capture_matplotlib_figure()

        def capture_plotly(*args: Any, **kwargs: Any) -> Any:
            return self._capture_plotly_figure(*args, **kwargs)

        self.plt.show = capture_matplotlib
        self.pio.show = capture_plotly
        self.pio.renderers.default = "capture"

        success = True
        error = None
        try:
            with redirect_stdout(stdout_buffer):
                self.user_globals.pop(_LAST_EXPR_GLOBAL, None)
                compiled_code, captures_last_expr = self._compile_code(code)
                exec(compiled_code, self.user_globals)
                if captures_last_expr:
                    self._print_last_expression_value(stdout_buffer)
        except BaseException:
            # SystemExit/KeyboardInterrupt in user code are failed executions
            # too; retain stdout and run the same artifact cleanup.
            success = False
            error = self._print_short_traceback(stdout_buffer)
        finally:
            self.plt.show = original_plt_show
            self.pio.show = original_pio_show
            self.pio.renderers.default = original_renderer

        images = self.images.copy()
        plotly = self.plotly.copy()
        records = self.records.copy()
        reports = self.reports.copy()
        if not success:
            self._delete_artifacts(images + plotly)
            images = []
            plotly = []
            records = []
            reports = []
            self.files.clear()
        if execution_id is None:
            try:
                self.files = self.file_store.publish(self.execution_id, self.files)
            finally:
                self.file_store.discard(self.execution_id)

        return {
            "ok": success,
            "error": error,
            "stdout": self._trim_stdout(stdout_buffer.getvalue()),
            "images": images,
            "plotly": plotly,
            "files": self.files.copy(),
            "wot_calls": self.wot_calls.copy(),
            "records": records,
            "reports": reports,
        }

    @staticmethod
    def _compile_code(code: str) -> tuple[Any, bool]:
        tree = ast.parse(code, mode="exec")
        captures_last_expr = bool(tree.body and isinstance(tree.body[-1], ast.Expr))
        if captures_last_expr:
            last_expr = tree.body[-1]
            if isinstance(last_expr, ast.Expr):
                tree.body[-1] = ast.copy_location(
                    ast.Assign(
                        targets=[ast.Name(id=_LAST_EXPR_GLOBAL, ctx=ast.Store())],
                        value=last_expr.value,
                    ),
                    last_expr,
                )
                ast.fix_missing_locations(tree)
        return compile(tree, "<string>", "exec"), captures_last_expr

    def _print_last_expression_value(self, stdout_buffer: io.StringIO) -> None:
        if (
            stdout_buffer.getvalue().strip()
            or self.reports
            or self.images
            or self.plotly
            or self.files
        ):
            return

        value = self.user_globals.get(_LAST_EXPR_GLOBAL)
        if value is None:
            return
        print(repr(value))

    @staticmethod
    def _prepare_process_environment() -> None:
        for key in SENSITIVE_ENV_VARS:
            os.environ.pop(key, None)

        os.environ["MPLBACKEND"] = "Agg"

    def _load_runtime_modules(self) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd
        import plotly.io as pio
        import requests

        self.plt = plt
        self.np = np
        self.pd = pd
        self.pio = pio
        self.requests = requests

    def _install_plotly_renderer(self) -> None:
        from plotly.io._base_renderers import ExternalRenderer

        environment = self

        class _CaptureRenderer(ExternalRenderer):
            def render(self, fig_dict):
                import plotly.graph_objects as go

                fig = go.Figure(fig_dict)
                environment._save_plotly_figure(fig)

        self.pio.renderers["capture"] = _CaptureRenderer()

    def _register_wot_module(self) -> None:
        wot_module = types.ModuleType("wot")
        wot_module.read_property = self.wot.read_property
        wot_module.write_property = self.wot.write_property
        wot_module.invoke_action = self.wot.invoke_action
        sys.modules["wot"] = wot_module

    def _build_user_globals(self) -> dict[str, Any]:
        return {
            "__builtins__": __builtins__,
            "pd": self.pd,
            "np": self.np,
            "plt": self.plt,
            "requests": self.requests,
            "print": print,
            "pio": self.pio,
            "save_image": self.save_image,
            "save_artifact": self.save_artifact,
            "store_record": self.store_record,
            "report": self.report,
            "wot": self.wot,
        }

    def save_artifact(
        self,
        data: Any,
        *,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Save bytes, UTF-8 text or a binary stream; publish only after success."""
        artifact = self.file_store.save(
            self.execution_id,
            data,
            filename=filename,
            mime_type=mime_type,
        )
        self.files.append(artifact)
        return artifact

    def report(self, message: Any) -> None:
        """Set the human-facing headline for an analysis run.

        Lets deterministic analysis code emit a clean, human-readable summary
        (e.g. "Line 3 processed 124 units") without an LLM. wotbot prefers this
        over raw stdout when surfacing the run in toasts and notifications. May be
        called more than once; the messages are joined in order. Reports are
        discarded if the run fails, mirroring artifacts and records.
        """
        if not isinstance(message, str):
            raise TypeError(f"report expects a string. Got {type(message)}")
        self.reports.append(message)

    def store_record(
        self,
        data: Any,
        *,
        raw_input: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Queue one structured record for a structured-record analysis job.

        The record is only validated against the job's schema and persisted by
        wotbot after the run succeeds (mirroring image/plotly artifacts, which
        are discarded on failure). The sandbox merely collects the raw payload.
        """
        if not isinstance(data, dict):
            raise TypeError(f"store_record expects a dict for data. Got {type(data)}")
        self.records.append(
            {
                "data": data,
                "raw_input": raw_input,
                "confidence": confidence,
            }
        )

    def _capture_matplotlib_figure(self) -> None:
        filename = f"{uuid.uuid4()}.png"
        filepath = os.path.join(self.artifacts_dir, filename)
        self.plt.savefig(filepath, format="png", bbox_inches="tight", dpi=150)
        self.images.append(filename)
        self.plt.close("all")

    def save_image(self, image: Any) -> None:
        filename = f"{uuid.uuid4()}.png"
        filepath = os.path.join(self.artifacts_dir, filename)
        if hasattr(image, "save"):
            image.save(filepath, format="PNG")
        elif hasattr(image, "read"):
            with open(filepath, "wb") as f:
                f.write(image.read())
        elif isinstance(image, bytes):
            with open(filepath, "wb") as f:
                f.write(image)
        else:
            raise TypeError(
                f"save_image expects PIL Image, BytesIO, or bytes. Got {type(image)}"
            )
        self.images.append(filename)

    def _capture_plotly_figure(self, fig: Any, *args: Any, **kwargs: Any) -> None:
        self._save_plotly_figure(fig)

    def _save_plotly_figure(self, fig: Any) -> None:
        filename = f"{uuid.uuid4()}.json"
        filepath = os.path.join(self.artifacts_dir, filename)
        if hasattr(fig, "write_json"):
            fig.write_json(filepath)
            self.plotly.append(filename)

    def _delete_artifacts(self, filenames: list[str]) -> None:
        for filename in filenames:
            filepath = os.path.join(self.artifacts_dir, filename)
            try:
                os.remove(filepath)
            except OSError:
                pass

    @staticmethod
    def _print_short_traceback(stdout_buffer: io.StringIO) -> str:
        with redirect_stdout(stdout_buffer):
            tb = traceback.format_exc()
            lines = tb.strip().splitlines()
            error_line = lines[-1] if lines else "Unknown error"
            source_line = ""
            for i, line in enumerate(lines):
                if 'File "<string>"' in line and i + 1 < len(lines):
                    source_line = lines[i + 1].strip()
            if source_line:
                print(f"Error: {error_line}\nAt: {source_line}")
            else:
                print(f"Error: {error_line}")
            return error_line[:MAX_STDOUT_CHARS]

    @staticmethod
    def _trim_stdout(stdout: str) -> str:
        if len(stdout) <= MAX_STDOUT_CHARS:
            return stdout

        trimmed = (
            stdout[:MAX_STDOUT_CHARS]
            + f"\n\n... truncated ({len(stdout)} chars total)."
            " Print only summaries, not raw data."
        )
        last_machine_line = _last_machine_readable_stdout_line(stdout)
        if last_machine_line is None:
            return trimmed

        if last_machine_line in trimmed.splitlines():
            return trimmed
        return f"{trimmed}\n{last_machine_line}"


def _last_machine_readable_stdout_line(stdout: str) -> str | None:
    last_start = -1
    for prefix in _MACHINE_READABLE_STDOUT_PREFIXES:
        start = stdout.rfind(f"\n{prefix}")
        if start >= 0:
            start += 1
        elif stdout.startswith(prefix):
            start = 0
        else:
            continue
        last_start = max(last_start, start)

    if last_start < 0:
        return None

    end = stdout.find("\n", last_start)
    if end < 0:
        return stdout[last_start:]
    return stdout[last_start:end].rstrip("\r")
