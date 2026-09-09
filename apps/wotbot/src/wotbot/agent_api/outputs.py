"""Capture outputs from successful executed tools, independently of assistant prose."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from wotbot.agent_api.artifacts import ArtifactStore
from wotbot.agent_api.constants import MCP_APP_MIME_TYPE
from wotbot.agent_api.types import Artifact, Part
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.panels.models import PanelVersion
from wotbot.panels.service import PanelService

logger = logging.getLogger(__name__)


def json_part(data):
    return Part(data=data, media_type="application/json")


def _executor_identifier(item: dict) -> str:
    identifier = item.get("id") or item.get("filename")
    if (
        not isinstance(identifier, str)
        or not identifier
        or any(c in identifier for c in ("/", "\\", ".."))
    ):
        raise ValueError("Invalid exported artifact identifier")
    return identifier


def _executor_url(settings, identifier: str, suffix: str) -> str:
    return (
        f"{settings.code_executor_url.rstrip('/')}/artifacts/{quote(identifier, safe='')}{suffix}"
    )


def executor_headers(settings) -> dict[str, str]:
    return (
        {"Authorization": f"Bearer {settings.internal_api_key}"}
        if settings.internal_api_key
        else {}
    )


async def fetch_artifact_metadata(settings, item: dict) -> dict:
    """Describe an exported artifact without copying it.

    The executor already stores the bytes with its own retention, so A2A keeps
    only what it needs to advertise and authorize a download: identity, type,
    size and the moment the bytes actually disappear.
    """
    identifier = _executor_identifier(item)
    async with httpx.AsyncClient(timeout=settings.code_executor_timeout_seconds) as client:
        response = await client.get(
            _executor_url(settings, identifier, "/metadata"), headers=executor_headers(settings)
        )
        response.raise_for_status()
        descriptor = response.json()
    if not isinstance(descriptor, dict) or not descriptor.get("expires_at"):
        raise ValueError("Executor returned an unusable artifact descriptor")
    return {**descriptor, "id": identifier}


async def fetch_plotly_figure(settings, item: dict) -> dict:
    """Read a Plotly figure so it can travel inline in the artifact.

    The only class whose bytes are still read: a figure is small, and a client
    that cannot follow the download URL still needs the data to render.
    """
    identifier = _executor_identifier(item)
    async with httpx.AsyncClient(timeout=settings.code_executor_timeout_seconds) as client:
        async with client.stream(
            "GET",
            _executor_url(settings, identifier, "/content"),
            headers=executor_headers(settings),
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > settings.a2a_max_artifact_bytes:
                    raise ValueError("Export exceeds A2A_MAX_ARTIFACT_BYTES")
    # A mislabeled HTML response is an export failure.
    return json.loads(bytes(content))


class ArtifactCollector:
    def __init__(
        self,
        *,
        settings,
        owner,
        task_id,
        thread_id,
        store=None,
        fetch=fetch_artifact_metadata,
        session_factory=None,
    ):
        self.settings, self.owner, self.task_id, self.thread_id = (
            settings,
            owner,
            task_id,
            thread_id,
        )
        self.store = store or ArtifactStore(session_factory)
        self.session_factory = session_factory or get_session_factory()
        self.fetch = fetch
        self.calls = {}
        self.processed = set()
        self.failures: list[str] = []

    async def consume(self, event, *, seen: set) -> list[Artifact]:
        def messages(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "messages" and isinstance(nested, (list, tuple)):
                        yield from nested
                    elif isinstance(nested, dict):
                        yield from messages(nested)

        if event.mode not in {"values", "updates"}:
            return []
        batch = list(messages(event.payload))
        for message in batch:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    self.calls[call["id"]] = call
        artifacts = []
        for message in batch:
            if not isinstance(message, ToolMessage) or message.id in seen:
                continue
            call = self.calls.get(message.tool_call_id)
            if not call or message.tool_call_id in self.processed:
                continue
            self.processed.add(message.tool_call_id)
            if message.status == "error":
                continue
            try:
                result = (
                    json.loads(message.content)
                    if isinstance(message.content, str)
                    else message.content
                )
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and (
                result.get("error")
                or result.get("ok") is False
                or result.get("status") in {"error", "stopped", "failed"}
            ):
                continue
            artifacts.extend(
                await self.record_tool_result(
                    call["name"], call["args"], result, message.tool_call_id
                )
            )
        return artifacts

    async def record_tool_result(self, tool_name, arguments, result, call_id):
        if isinstance(result, dict) and (
            result.get("error")
            or result.get("ok") is False
            or result.get("status") in {"error", "failed", "stopped"}
        ):
            return []
        artifacts = []
        items = result.get("artifacts", []) if isinstance(result, dict) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            artifact_id = str(uuid5(NAMESPACE_URL, f"wotbot:{self.task_id}:{call_id}:{index}"))
            try:
                if item.get("kind") == "web" and tool_name == "create_web_interface":
                    artifacts.append(await self._panel(artifact_id, arguments, item))
                else:
                    artifacts.append(await self._file(artifact_id, item))
            except Exception:
                logger.exception("A2A export failed task=%s artifact=%s", self.task_id, artifact_id)
                self.failures.append(f"{item.get('kind', 'file')} export {artifact_id} failed")
        # Tool result data is a separate artifact with an explicit stable kind.
        # Generated-file descriptors are replaced by the durable manifests above.
        data = (
            {k: v for k, v in result.items() if k != "artifacts"}
            if isinstance(result, dict)
            else result
        )
        if data is not None and data != {}:
            identifier = str(uuid5(NAMESPACE_URL, f"wotbot:{self.task_id}:{call_id}:result"))
            artifacts.append(
                Artifact(
                    artifact_id=identifier,
                    name=tool_name + " result",
                    parts=[
                        json_part({"kind": "wotbot.tool_result", "tool": tool_name, "result": data})
                    ],
                )
            )
        for artifact in artifacts:
            if not any(part.url for part in artifact.parts) and not any(
                isinstance(part.data, dict) and part.data.get("kind") == "wotbot.panel"
                for part in artifact.parts
            ):
                await self.store.put(
                    artifact_id=artifact.artifact_id,
                    task_id=self.task_id,
                    owner=self.owner,
                    name=artifact.name,
                    media_type="application/json",
                    metadata={
                        "kind": "json",
                        "artifact": artifact.json(),
                        "expiresAt": (
                            utc_now() + timedelta(days=self.settings.a2a_task_retention_days)
                        ).isoformat(),
                    },
                )
        return artifacts

    async def _file(self, artifact_id, item):
        descriptor = await self.fetch(self.settings, item)
        media_type = descriptor.get("mime_type") or "application/octet-stream"
        name = descriptor.get("filename") or descriptor["id"]
        # The link cannot outlive the bytes, and A2A's own retention only ever
        # shortens it further.
        expiry = min(
            datetime.fromisoformat(descriptor["expires_at"]),
            utc_now() + timedelta(days=self.settings.a2a_artifact_retention_days),
        )
        url = f"{self.settings.registry_public_url.rstrip('/')}/a2a/artifacts/{artifact_id}"
        figure = None
        if item.get("kind") == "plotly":
            media_type = "application/vnd.plotly.v1+json"
            figure = await fetch_plotly_figure(self.settings, item)
        metadata = {
            "kind": item.get("kind", "file"),
            "filename": name,
            "mimeType": media_type,
            "sizeBytes": descriptor.get("size_bytes"),
            "expiresAt": expiry.isoformat(),
            "downloadUrl": url,
        }
        if figure is not None:
            metadata["figure"] = figure
            metadata["resultKind"] = "wotbot.plotly"
        await self.store.put(
            artifact_id=artifact_id,
            task_id=self.task_id,
            owner=self.owner,
            name=name,
            media_type=media_type,
            metadata=metadata,
            executor_artifact_id=descriptor["id"],
        )
        logger.info(
            "Recorded A2A artifact=%s executor=%s task=%s",
            artifact_id,
            descriptor["id"],
            self.task_id,
        )
        parts = [Part(url=url, media_type=media_type, filename=name)]
        if figure is not None:
            parts.append(json_part({"kind": "wotbot.plotly", "figure": figure}))
        return Artifact(artifact_id=artifact_id, name=name, parts=parts, metadata=metadata)

    async def _panel(self, artifact_id, inputs, result):
        from wotbot.mcp_apps.render import panel_ui_metadata

        existing = await self.store.get(artifact_id, owner=self.owner)
        if existing:
            descriptor = existing.artifact_metadata["descriptor"]
        else:
            html = inputs.get("html")
            if not isinstance(html, str) or not html.strip():
                raise ValueError("Successful panel has no matching raw tool input")
            capabilities = result.get("capabilities") or []

            # Only a successful validated tool result reaches here; save its exact
            # inputs and normalized capabilities through the normal panel service.
            def save_panel():
                with self.session_factory() as session:
                    panel = PanelService(session).create_panel(
                        title=inputs.get("title", ""),
                        html=html,
                        capabilities=capabilities,
                        source_thread_id=self.thread_id,
                    )
                    version = session.scalar(
                        select(PanelVersion).where(
                            PanelVersion.panel_id == panel["id"], PanelVersion.version_number == 1
                        )
                    )
                    return panel, version.id

            panel, version_id = await asyncio.to_thread(save_panel)
            descriptor = {
                "kind": "wotbot.panel",
                "version": 1,
                "panelId": panel["id"],
                "panelVersionId": version_id,
                "panelUrl": f"{self.settings.public_ui_origin.rstrip('/')}/panels?panelId={panel['id']}",
                "mcpServerUrl": self.settings.registry_public_url.rstrip("/") + "/mcp/apps",
                "toolName": f"panel.open.{artifact_id}",
                "resourceUri": f"ui://wotbot/generated/{artifact_id}",
            }
            # The markup and its capability allowlist live in the pinned panel
            # version; the document is re-wrapped from there on read, so the
            # panel picks up later bridge and CSP fixes instead of freezing the
            # ones in force when it was generated. `ui` is cached here only as
            # a listing hint -- reads recompute it alongside the document.
            await self.store.put(
                artifact_id=artifact_id,
                task_id=self.task_id,
                owner=self.owner,
                name=panel["title"],
                media_type=MCP_APP_MIME_TYPE,
                metadata={
                    "bridgeVersion": 2,
                    "panelVersionId": version_id,
                    "ui": panel_ui_metadata(self.settings, html),
                    "descriptor": descriptor,
                },
            )
            logger.info(
                "Saved A2A panel=%s version=%s artifact=%s task=%s",
                panel["id"],
                version_id,
                artifact_id,
                self.task_id,
            )
        return Artifact(
            artifact_id=artifact_id, name="Generated panel", parts=[json_part(descriptor)]
        )
