"""Ordinary MCP tools for assistant, intent, and direct tool execution."""

import asyncio
import base64
import json
import logging
from datetime import datetime

import httpx
from jsonschema import Draft202012Validator
from mcp.server.apps import client_supports_apps
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    ReadResourceResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
    Tool,
)

from wotbot.agent.intents import INTENTS
from wotbot.agent_api.catalog import argument_schema, public_tools, validate_arguments
from wotbot.agent_api.downloads import ArtifactDownloadLinks
from wotbot.agent_api.errors import AgentError
from wotbot.agent_api.types import Operation, TaskQuery
from wotbot.mcp_apps.server import (
    PANEL_CALL_TOOL,
    PANEL_TOOL_PREFIX,
    MCPPanelRuntime,
    _panel_call_tool,
    _panel_open_tool,
    _request_user,
    _tool_error,
)

ID = {"type": "string", "minLength": 1, "maxLength": 200}
WAIT = {"type": "number", "minimum": 0, "maximum": 30, "default": 25}
COMMON = {"requestId": ID, "contextId": ID, "waitSeconds": WAIT}


def schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def invocation_schema(arguments=None):
    fields = dict(COMMON)
    if arguments is None:
        fields.update(message={"type": "string", "minLength": 1}, data={})
        result = schema(fields, ["requestId"])
        result["anyOf"] = [{"required": ["message"]}, {"required": ["data"]}]
    else:
        arguments = dict(arguments)
        definitions = arguments.pop("$defs", {})
        fields["arguments"] = arguments
        result = schema(fields, ["requestId", "arguments"])
        if definitions:
            result["$defs"] = definitions
    return result


def profile_tools(profile):
    if profile == "assistant":
        tools = [
            Tool(
                name="ask_wotbot",
                description="Ask WoTBot to discover, control, analyze, or automate Things. The assistant selects the intent. Reuse contextId to continue; retry the same requestId only with identical input.",
                input_schema=invocation_schema(),
            )
        ]
    elif profile == "intents":
        tools = [
            Tool(
                name="intent." + intent.id,
                description=intent.description
                + " Enters this assistant branch; configured handoffs still apply.",
                input_schema=invocation_schema(),
            )
            for intent in INTENTS.values()
        ]
    elif profile == "raw":
        tools = [
            Tool(
                name=name,
                description=tool.description,
                input_schema=invocation_schema(argument_schema(name)),
            )
            for name, tool in public_tools().items()
        ]
    else:
        raise ValueError("Unknown MCP profile")
    utilities = [
        (
            "task.get",
            "Retrieve a task; optionally wait up to 30 seconds for completion or input.",
            schema({"taskId": ID, "waitSeconds": {**WAIT, "default": 0}}, ["taskId"]),
        ),
        (
            "task.list",
            "List this API key's tasks in this execution family.",
            schema({"contextId": ID, "cursor": {"type": "string", "maxLength": 2048}}),
        ),
        (
            "task.resume",
            "Resume a paused task after supplying every requested reply. Provision credentials through the credential API, never in these replies.",
            schema(
                {
                    "taskId": ID,
                    "requestId": ID,
                    "waitSeconds": WAIT,
                    "replies": {
                        "type": "array",
                        "minItems": 1,
                        "items": schema(
                            {"requestId": ID, "response": {}}, ["requestId", "response"]
                        ),
                    },
                },
                ["taskId", "requestId", "replies"],
            ),
        ),
        (
            "task.cancel",
            "Stop a task and finish checkpoint cleanup. Completed device actions are not undone.",
            schema({"taskId": ID}, ["taskId"]),
        ),
        (
            "artifact.get",
            "Retrieve an owned artifact with fresh temporary download links. Open generated panels using their panel.open tool.",
            schema({"artifactId": ID}, ["artifactId"]),
        ),
        (
            "artifact.list",
            "List this API key's artifacts, optionally filtered by task or context.",
            schema(
                {"taskId": ID, "contextId": ID, "cursor": {"type": "string", "maxLength": 2048}}
            ),
        ),
    ]
    if profile == "raw":
        utilities.append(
            (
                "subscription.poll",
                "Read the next event from an owned raw subscription and renew its one-hour idle lease. Save nextCursor for the following call.",
                schema(
                    {
                        "contextId": ID,
                        "subscriptionId": ID,
                        "cursor": {"type": "string"},
                        "timeoutMs": {"type": "integer", "minimum": 1, "maximum": 30000},
                    },
                    ["contextId", "subscriptionId"],
                ),
            )
        )
    tools += [
        Tool(name=name, description=description, input_schema=definition)
        for name, description, definition in utilities
    ]
    return {tool.name: tool for tool in tools}


class MCPToolRuntime(MCPPanelRuntime):
    def __init__(self, *, profile, get_runtime, **kwargs):
        self.profile = profile
        self.get_runtime = get_runtime
        self.tools = profile_tools(profile)
        super().__init__(path="/mcp/" + profile, **kwargs)
        self.downloads = ArtifactDownloadLinks(self.settings, artifact_store=self.artifact_store)
        self.server.instructions = (
            "Execution calls return a completed result, an input request, or a durable task handle. "
            "Use task.get to wait again, task.resume for pending replies, and task.cancel to stop. "
            "Connections do not own task lifetime. Keep requestId unchanged for an identical retry. "
            "Every context belongs to this API key; raw contexts are separate from assistant conversations."
        )

    @property
    def runtime(self):
        runtime = self.get_runtime()
        if runtime is None:
            raise ValueError("Assistant is not ready")
        return runtime

    @property
    def family(self):
        return "raw" if self.profile == "raw" else "assistant"

    def _cursor(self, token, owner, purpose):
        if not token:
            return None
        try:
            if len(token) > 2048:
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            if payload[:3] != [owner, self.profile, purpose]:
                raise ValueError()
            timestamp, identifier = payload[3:]
            if not isinstance(identifier, str) or len(identifier) > 200:
                raise ValueError()
            timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is None:
                raise ValueError()
            return timestamp, identifier
        except (ValueError, TypeError):
            raise ValueError("Invalid cursor for this query") from None

    def _next(self, row, owner, purpose):
        return (
            base64.urlsafe_b64encode(
                json.dumps(
                    [owner, self.profile, purpose, row.created_at.isoformat(), row.id]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )

    async def _list_tools(self, ctx, params):
        owner = _request_user().api_key_id
        supports_apps = client_supports_apps(ctx)
        static = list(self.tools.values()) + ([_panel_call_tool()] if supports_apps else [])
        offset, before = 0, None
        token = params.cursor if params else None
        try:
            if token:
                if len(token) > 2048:
                    raise ValueError()
                binding, offset, position = json.loads(
                    base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                )
                if binding != [owner, self.profile, supports_apps]:
                    raise ValueError()
                if type(offset) is not int or not 0 <= offset <= len(static):
                    raise ValueError()
                if position:
                    before = self._cursor(position, owner, "tools")
            tools = static[offset : offset + 50]
            offset += len(tools)
            remaining = 50 - len(tools)
            page = await self.artifact_store.list_mcp_apps(
                owner=owner, limit=remaining or 1, before=before
            )
            if remaining:
                tools += [_panel_open_tool(a, supports_apps=supports_apps) for a in page.items]
                if page.items:
                    position = self._next(page.items[-1], owner, "tools")
                else:
                    position = None
                more = page.has_more
            else:
                position = self._next_cursor_position(before, owner)
                more = offset < len(static) or bool(page.items)
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps([[owner, self.profile, supports_apps], offset, position]).encode()
                )
                .decode()
                .rstrip("=")
                if more
                else None
            )
            return ListToolsResult(
                tools=tools, next_cursor=next_cursor, cache_scope="private", ttl_ms=0
            )
        except (ValueError, TypeError) as error:
            raise MCPError(INVALID_PARAMS, "Invalid tool-list cursor") from error

    def _next_cursor_position(self, before, owner):
        if before is None:
            return None
        timestamp, identifier = before
        return (
            base64.urlsafe_b64encode(
                json.dumps(
                    [owner, self.profile, "tools", timestamp.isoformat(), identifier]
                ).encode()
            )
            .decode()
            .rstrip("=")
        )

    async def _call_tool(self, ctx, params):
        user = _request_user()
        name, arguments = params.name, params.arguments or {}
        if name.startswith(PANEL_TOOL_PREFIX) or name == PANEL_CALL_TOOL:
            return await super()._call_tool(ctx, params)
        tool = self.tools.get(name)
        if tool is None:
            return _tool_error("Unknown tool for this MCP profile")
        if list(Draft202012Validator(tool.input_schema).iter_errors(arguments)):
            return _tool_error("Arguments do not match the published tool schema; no operation ran")
        try:
            if len(json.dumps(arguments)) > 1_000_000:
                raise ValueError("Request is too large")
            owner = user.api_key_id
            if name == "artifact.get":
                descriptor = await self._artifact(owner, arguments["artifactId"])
                return await self._result(descriptor, [descriptor])
            if name == "artifact.list":
                purpose = json.dumps(
                    ["artifacts", arguments.get("taskId"), arguments.get("contextId")]
                )
                page = await self.artifact_store.list_artifacts(
                    owner=owner,
                    task_id=arguments.get("taskId"),
                    context_id=arguments.get("contextId"),
                    before=self._cursor(arguments.get("cursor"), owner, purpose),
                )
                descriptors = []
                for row in page.items:
                    try:
                        descriptors.append(await self._artifact(owner, row.id))
                    except ValueError:
                        descriptors.append(
                            {"artifactId": row.id, "name": row.name, "expired": True}
                        )
                return await self._result(
                    {
                        "artifacts": descriptors,
                        "nextCursor": self._next(page.items[-1], owner, purpose)
                        if page.has_more
                        else None,
                    }
                )
            runtime = self.runtime
            if name == "subscription.poll":
                result = await runtime.subscriptions.poll(
                    owner,
                    arguments["contextId"],
                    arguments["subscriptionId"],
                    cursor=arguments.get("cursor"),
                    timeout_ms=arguments.get("timeoutMs", 25000),
                )
                return await self._result(result)
            if name == "task.list":
                tasks, total, cursor = await asyncio.to_thread(
                    runtime.store.list,
                    owner,
                    TaskQuery(
                        context_id=arguments.get("contextId", ""),
                        page_token=arguments.get("cursor", ""),
                        family=self.family,
                    ),
                )
                return await self._result(
                    {
                        "tasks": [await self._snapshot(owner, task) for task in tasks],
                        "total": total,
                        "nextCursor": cursor or None,
                    }
                )
            if name.startswith("task."):
                await asyncio.to_thread(
                    runtime.store.require_family, owner, arguments["taskId"], self.family
                )
            if name == "task.cancel":
                task = await asyncio.shield(runtime.cancel(owner, arguments["taskId"]))
            elif name == "task.get":
                task = await runtime.wait(
                    owner, arguments["taskId"], arguments.get("waitSeconds", 0)
                )
            else:
                if name == "task.resume":
                    message = {
                        "messageId": arguments["requestId"],
                        "role": "ROLE_USER",
                        "taskId": arguments["taskId"],
                        "parts": [{"data": reply} for reply in arguments["replies"]],
                    }
                    operation = await asyncio.to_thread(
                        runtime.store.operation, owner, arguments["taskId"]
                    )
                else:
                    message = {
                        "messageId": arguments["requestId"],
                        "role": "ROLE_USER",
                        "parts": [],
                    }
                    if arguments.get("contextId"):
                        message["contextId"] = arguments["contextId"]
                    if self.profile == "raw":
                        validate_arguments(name, arguments["arguments"])
                        operation = Operation(
                            kind="raw", name=name, arguments=arguments["arguments"]
                        )
                        message["parts"] = [
                            {"data": {"tool": name, "arguments": arguments["arguments"]}}
                        ]
                    else:
                        operation = (
                            Operation(kind="intent", name=name.removeprefix("intent."))
                            if self.profile == "intents"
                            else Operation()
                        )
                        if "message" in arguments:
                            message["parts"].append({"text": arguments["message"]})
                        if "data" in arguments:
                            message["parts"].append({"data": arguments["data"]})
                request = {"message": message, "origin": "mcp:" + self.profile}
                if operation.kind != "assistant":
                    request["operation"] = operation.json()
                admission = await asyncio.shield(runtime.admit(owner, request))
                task = await runtime.wait(
                    owner, admission.task.id, arguments.get("waitSeconds", 25)
                )
            snapshot = await self._snapshot(owner, task)
            return await self._result(
                snapshot, snapshot["artifacts"], error=snapshot["status"] in {"failed", "rejected"}
            )
        except (ValueError, AgentError, PermissionError) as error:
            return _tool_error(str(error))

    async def _artifact(self, owner, identifier):
        descriptor = await self.downloads.descriptor(owner, identifier)
        descriptor["resourceUri"] = "wotbot://artifacts/" + identifier
        if descriptor.get("descriptor"):
            descriptor["descriptor"] = {
                **descriptor["descriptor"],
                "mcpServerUrl": self.settings.registry_public_url.rstrip("/") + self.path,
            }
        return descriptor

    async def _snapshot(self, owner, task):
        artifacts = []
        for artifact in task.artifacts:
            try:
                artifacts.append(await self._artifact(owner, artifact.artifact_id))
            except ValueError as error:
                artifacts.append({**artifact.json(), "error": str(error)})
        message = task.status.message.json() if task.status.message else None
        pending = []
        if task.status.message:
            for part in task.status.message.parts:
                if isinstance(part.data, dict) and part.data.get("kind") == "wotbot.input_requests":
                    pending = part.data["requests"]
        result = task.metadata.get("wotbotResult")
        if isinstance(result, dict) and "artifacts" in result:
            result = {**result, "artifacts": artifacts}
        return {
            "taskId": task.id,
            "contextId": task.context_id,
            "status": task.status.state.value.removeprefix("TASK_STATE_").lower(),
            "messages": [m.json() for m in task.history],
            "statusMessage": message,
            "result": result,
            "artifacts": artifacts,
            "pending": pending,
            "error": message
            if task.status.state.value in {"TASK_STATE_FAILED", "TASK_STATE_REJECTED"}
            else None,
        }

    async def _result(self, data, artifacts=(), *, error=False):
        content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]
        for artifact in artifacts:
            uri = artifact.get("resourceUri") or artifact.get("downloadUrl")
            if uri and not artifact.get("error"):
                content.append(
                    ResourceLink(
                        type="resource_link",
                        uri=uri,
                        name=artifact.get("name", "Artifact"),
                        mime_type="application/json",
                    )
                )
        preview_budget = 1024 * 1024
        for artifact in artifacts:
            if not artifact.get("mimeType", "").startswith("image/") or artifact.get("error"):
                continue
            size = artifact.get("sizeBytes")
            if not isinstance(size, int) or size > preview_budget:
                continue
            try:
                from wotbot.agent_api.outputs import _executor_url, executor_headers

                record = await self.artifact_store.get(
                    artifact["artifactId"], owner=_request_user().api_key_id
                )
                if not record or not record.executor_artifact_id:
                    continue
                image_bytes = bytearray()
                async with httpx.AsyncClient(
                    timeout=self.settings.code_executor_timeout_seconds
                ) as client:
                    async with client.stream(
                        "GET",
                        _executor_url(self.settings, record.executor_artifact_id, "/content"),
                        headers=executor_headers(self.settings),
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            image_bytes.extend(chunk)
                            if len(image_bytes) > preview_budget:
                                raise ValueError("Image exceeds inline preview limit")
                content.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(image_bytes).decode(),
                        mime_type=artifact["mimeType"],
                    )
                )
                preview_budget -= len(image_bytes)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Image preview unavailable artifact=%s", artifact.get("artifactId")
                )
                content.append(
                    TextContent(
                        type="text",
                        text="Image preview unavailable; use the artifact download link.",
                    )
                )
        return CallToolResult(content=content, structured_content=data, is_error=error)

    async def _read_resource(self, ctx, params):
        prefix = "wotbot://artifacts/"
        if str(params.uri).startswith(prefix):
            descriptor = await self._artifact(
                _request_user().api_key_id, str(params.uri)[len(prefix) :]
            )
            return ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=params.uri, mime_type="application/json", text=json.dumps(descriptor)
                    )
                ],
                cache_scope="private",
                ttl_ms=0,
            )
        return await super()._read_resource(ctx, params)
