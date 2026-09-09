from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx

from wotbot.core.settings import Settings

logger = logging.getLogger(__name__)


class CodeExecutionUncertainError(RuntimeError):
    """The executor may have run the program before its response was lost."""

    def __init__(self) -> None:
        super().__init__(
            "The code execution outcome is unknown. The program may already have applied "
            "device actions; it was not automatically retried. Inspect current device state "
            "before deciding whether to run any part of the program again."
        )


def build_code_artifacts(images: list[str], plotly: list[str]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []

    for index, filename in enumerate(images, start=1):
        if not isinstance(filename, str) or not filename:
            continue
        artifacts.append(
            {
                "ref": f"image_{index}",
                "kind": "image",
                "filename": filename,
            }
        )

    for index, filename in enumerate(plotly, start=1):
        if not isinstance(filename, str) or not filename:
            continue
        artifacts.append(
            {
                "ref": f"chart_{index}",
                "kind": "plotly",
                "filename": filename,
            }
        )

    return artifacts


def format_code_execution_result(data: dict[str, Any]) -> dict[str, object]:
    stdout = str(data.get("stdout", "")).rstrip()
    raw_images = data.get("images", [])
    raw_plotly = data.get("plotly", [])
    artifacts = build_code_artifacts(
        raw_images if isinstance(raw_images, list) else [],
        raw_plotly if isinstance(raw_plotly, list) else [],
    )
    wot_calls = data.get("wot_calls", [])

    for index, artifact in enumerate(data.get("files", []) or [], start=1):
        if isinstance(artifact, dict) and isinstance(artifact.get("id"), str):
            artifacts.append({**artifact, "ref": f"file_{index}", "kind": "file"})

    result: dict[str, object] = {}
    if "ok" in data:
        result["ok"] = data["ok"]
    if data.get("error"):
        result["error"] = data["error"]
    if stdout:
        result["stdout"] = stdout
    if artifacts:
        result["artifacts"] = artifacts
    if wot_calls:
        result["wot_calls"] = wot_calls
    if not result:
        result["stdout"] = "(no output)"

    return result


class CodeExecutorClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.code_executor_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        token = self._settings.internal_api_key
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _artifact_url(self, identifier: str, suffix: str) -> str:
        return f"{self._base_url}/artifacts/{quote(identifier, safe='')}/{suffix}"

    async def artifact_metadata(self, identifier: str) -> dict:
        async with httpx.AsyncClient(
            timeout=self._settings.code_executor_timeout_seconds
        ) as client:
            response = await client.get(
                self._artifact_url(identifier, "metadata"), headers=self._headers()
            )
            response.raise_for_status()
            descriptor = response.json()
        if not isinstance(descriptor, dict) or not descriptor.get("expires_at"):
            raise ValueError("Executor returned an unusable artifact descriptor")
        return {**descriptor, "id": identifier}

    @asynccontextmanager
    async def stream_artifact(self, identifier: str):
        """Keep the upstream response open until its consumer finishes or disconnects."""
        async with httpx.AsyncClient(
            timeout=self._settings.code_executor_timeout_seconds
        ) as client:
            async with client.stream(
                "GET", self._artifact_url(identifier, "content"), headers=self._headers()
            ) as response:
                yield response

    async def read_artifact(self, identifier: str, *, max_bytes: int) -> bytes:
        async with self.stream_artifact(identifier) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise ValueError("Artifact exceeds content size limit")
                content.extend(chunk)
            return bytes(content)

    async def store_web_artifact(self, *, html: str) -> str:
        """Persist a generated HTML interface and return its artifact filename."""
        async with httpx.AsyncClient(
            timeout=self._settings.code_executor_timeout_seconds
        ) as client:
            response = await client.post(
                f"{self._base_url}/web-artifacts",
                json={"html": html},
                headers=self._headers(),
            )
            response.raise_for_status()
            body = response.json()
        filename = body.get("filename") if isinstance(body, dict) else None
        if not isinstance(filename, str) or not filename:
            raise RuntimeError("Code executor did not return a web artifact filename")
        return filename

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        attempts = max(1, self._settings.code_executor_retry_attempts)
        base_backoff = max(0.0, self._settings.code_executor_retry_backoff_seconds)

        last_error: Exception | None = None
        timeout = (
            self._settings.code_executor_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.post(
                        f"{self._base_url}/execute",
                        json={"session_id": session_id, "code": code},
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
                        raise CodeExecutionUncertainError()
                    return body
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    # These failures occur before sending a request. Read/write
                    # failures and HTTP errors can follow real device actions.
                    last_error = exc
                    if attempt >= attempts:
                        break
                    sleep_seconds = base_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "Code executor request failed (attempt %s/%s): %s; retrying in %.2fs",
                        attempt,
                        attempts,
                        exc,
                        sleep_seconds,
                    )
                    if sleep_seconds > 0:
                        await asyncio.sleep(sleep_seconds)
                except (httpx.TransportError, httpx.DecodingError) as exc:
                    raise CodeExecutionUncertainError() from exc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code >= 500 or exc.response.status_code == 408:
                        raise CodeExecutionUncertainError() from exc
                    raise
                except ValueError as exc:
                    # A malformed response says nothing about whether execution
                    # happened; never turn it into an empty successful result.
                    raise CodeExecutionUncertainError() from exc

        if last_error is not None:
            raise last_error
        raise RuntimeError("Code executor request failed without an explicit exception")
