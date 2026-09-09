"""Authenticated MCP resource server exposing generated panels as MCP Apps."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID, client_supports_apps
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)
from pydantic import AnyHttpUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wotbot.a2a.artifacts import ArtifactStore
from wotbot.a2a.constants import MCP_APP_MIME_TYPE
from wotbot.auth.models import User
from wotbot.auth.providers import get_api_key_user_from_token
from wotbot.core.settings import Settings
from wotbot.mcp_apps.grants import PanelGrantStore
from wotbot.mcp_apps.render import panel_ui_metadata, wrap_mcp_app_document
from wotbot.mcp_apps.service import (
    PanelActionService,
    close_runtime_stream_clients,
    load_pinned_version,
)

logger = logging.getLogger(__name__)
PANEL_TOOL_PREFIX = "panel.open."
PANEL_CALL_TOOL = "panels.call"
PANEL_RESOURCE_PREFIX = "ui://wotbot/generated/"
PANEL_LAUNCH_META_KEY = "dev.wotbot/panel-launch"
PAGE_SIZE = 50
PANEL_GRANT_TTL_SECONDS = 3600


class MCPRootEndpoint:
    """Expose the SDK app at an exact parent path without a slash redirect."""

    def __init__(self, app: ASGIApp, *, path: str) -> None:
        self.app = app
        self.path = path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        child_scope = dict(scope)
        child_scope["root_path"] = f"{scope.get('root_path', '')}{self.path}"
        child_scope["path"] = "/"
        child_scope["raw_path"] = b"/"
        await self.app(child_scope, receive, send)


class ApiKeyTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        user = await asyncio.to_thread(get_api_key_user_from_token, token)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=user.api_key_id,
            subject=user.api_key_id,
            scopes=list(user.scopes or []),
            claims={"auth_type": user.auth_type},
        )


class MCPPanelRuntime:
    def __init__(self, *, settings=None, artifact_store=None, grant_store=None, panel_service=None):
        self.settings = settings or Settings()
        self.artifact_store = artifact_store or ArtifactStore()
        self.grant_store = grant_store or PanelGrantStore(self.settings.redis_url)
        self.panel_service = panel_service or PanelActionService(self.artifact_store)
        self.server = Server(
            "wotbot-panels",
            version="1.0.0",
            title="WoTBot Panels",
            instructions="Open a saved generated panel. Panel interactions use panels.call from the app and do not start assistant tasks.",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_read_resource=self._read_resource,
        )
        self.server.extensions = {EXTENSION_ID: {"mimeTypes": [APP_MIME_TYPE]}}
        public = urlsplit(self.settings.registry_public_url)
        origins = [
            self.settings.registry_public_url.rstrip("/"),
            self.settings.public_ui_origin.rstrip("/"),
        ]
        origins += [o.strip() for o in self.settings.mcp_allowed_origins.split(",") if o.strip()]
        hosts = [
            public.netloc,
            *[h.strip() for h in self.settings.mcp_allowed_hosts.split(",") if h.strip()],
        ]
        self.asgi_app = self.server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=False,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(self.settings.registry_public_url),
                required_scopes=["agent:invoke"],
                resource_server_url=None,
            ),
            token_verifier=ApiKeyTokenVerifier(),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
            ),
        )
        self.asgi_app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Mcp-Protocol-Version",
                "Mcp-Session-Id",
            ],
            expose_headers=["Mcp-Session-Id"],
        )
        self.root_endpoint = MCPRootEndpoint(self.asgi_app, path="/mcp/apps")

    def install(self, app):
        # Preflight must reach this app's CORS middleware at the exact URL,
        # rather than receiving a parent-router 405 or a slash redirect.
        app.router.routes.append(
            Route(
                "/mcp/apps",
                self.root_endpoint,
                methods=["GET", "POST", "DELETE", "OPTIONS"],
            )
        )
        app.mount("/mcp/apps", self.asgi_app)

    @asynccontextmanager
    async def lifespan(self):
        async with self.asgi_app.router.lifespan_context(self.asgi_app):
            try:
                yield
            finally:
                await close_runtime_stream_clients()

    async def _list_tools(self, ctx, params):
        user = _request_user()
        page = await self.artifact_store.list_mcp_apps(
            owner=user.api_key_id,
            limit=PAGE_SIZE,
            before=_decode_cursor(params.cursor if params else None),
        )
        supports_apps = client_supports_apps(ctx)
        tools = [_panel_open_tool(a, supports_apps=supports_apps) for a in page.items]
        if supports_apps:
            tools.append(_panel_call_tool())
        return ListToolsResult(
            tools=tools,
            next_cursor=_next_cursor(page.items, page.has_more),
            cache_scope="private",
            ttl_ms=0,
        )

    async def _list_resources(self, ctx, params):
        user = _request_user()
        page = await self.artifact_store.list_mcp_apps(
            owner=user.api_key_id,
            limit=PAGE_SIZE,
            before=_decode_cursor(params.cursor if params else None),
        )
        return ListResourcesResult(
            resources=[_panel_resource(a) for a in page.items],
            next_cursor=_next_cursor(page.items, page.has_more),
            cache_scope="private",
            ttl_ms=0,
        )

    async def _read_resource(self, ctx, params):
        artifact = await self.artifact_store.get(
            _artifact_id_from_resource(params.uri), owner=_request_user().api_key_id
        )
        if not _is_compatible_panel(artifact):
            raise MCPError(INVALID_PARAMS, "Resource not found")
        pinned = await load_pinned_version((artifact.artifact_metadata or {}).get("panelVersionId"))
        if pinned is None:
            raise MCPError(INVALID_PARAMS, "Resource not found")
        # Wrapped here rather than at generation time, so a saved panel serves
        # the current bridge and CSP while still running its pinned markup.
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=str(params.uri),
                    mime_type=MCP_APP_MIME_TYPE,
                    text=wrap_mcp_app_document(pinned.html, pinned.title, settings=self.settings),
                    _meta={"ui": panel_ui_metadata(self.settings, pinned.html)},
                )
            ],
            cache_scope="private",
            ttl_ms=0,
        )

    async def _call_tool(self, ctx, params):
        user = _request_user()
        if params.name.startswith(PANEL_TOOL_PREFIX):
            if params.arguments:
                raise MCPError(INVALID_PARAMS, "Panel open does not accept arguments")
            return await self._open_panel(params.name, user)
        if params.name == PANEL_CALL_TOOL:
            return await self._run_panel_call(params.arguments or {}, user)
        raise MCPError(METHOD_NOT_FOUND, "Unknown tool")

    async def _open_panel(self, tool_name: str, user: User) -> CallToolResult:
        artifact_id = _artifact_id_from_tool(tool_name)
        artifact = await self.artifact_store.get(artifact_id, owner=user.api_key_id)
        if artifact is None or not _is_compatible_panel(artifact):
            raise MCPError(METHOD_NOT_FOUND, f"Unknown tool: {tool_name}")
        grant = await self.grant_store.issue(
            artifact_id=artifact_id,
            owner=user.api_key_id,
            ttl_seconds=PANEL_GRANT_TTL_SECONDS,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=f"Opened generated panel: {artifact.name}")],
            structured_content={
                "artifactId": artifact_id,
                "title": artifact.name,
                **artifact.artifact_metadata.get("descriptor", {}),
            },
            _meta={
                PANEL_LAUNCH_META_KEY: {
                    "grant": grant.token,
                    "artifactId": artifact_id,
                    "expiresAt": grant.expires_at.isoformat(),
                    "callTool": PANEL_CALL_TOOL,
                }
            },
        )

    async def _run_panel_call(self, arguments: dict[str, Any], user: User) -> CallToolResult:
        if set(arguments) != {"grant", "tool", "arguments"}:
            return _tool_error("Panel authorization failed")
        grant_token = arguments.get("grant")
        tool_name = arguments.get("tool")
        tool_arguments = arguments.get("arguments", {})
        if (
            not isinstance(grant_token, str)
            or not isinstance(tool_name, str)
            or not isinstance(tool_arguments, dict)
        ):
            return _tool_error("Panel authorization failed")
        grant = await self.grant_store.resolve(grant_token, owner=user.api_key_id)
        if grant is None:
            return _tool_error("Panel authorization failed")
        try:
            result = await self.panel_service.execute(
                artifact_id=grant.artifact_id,
                owner=user.api_key_id,
                user=user,
                tool_name=tool_name,
                arguments=tool_arguments,
            )
        except (PermissionError, ValueError):
            return _tool_error("Panel operation is not authorized")
        except Exception:
            logger.exception("MCP panel operation failed")
            return _tool_error("Panel operation failed")
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, default=str))],
            structured_content={"result": result},
        )


def _request_user() -> User:
    access_token = get_access_token()
    if access_token is None:
        raise MCPError(INVALID_PARAMS, "Authentication required")
    owner = access_token.subject or access_token.client_id
    return User(
        user_id=owner,
        api_key_id=owner,
        scopes=list(access_token.scopes),
        auth_type=str((access_token.claims or {}).get("auth_type") or "api_key"),
    )


def _is_compatible_panel(artifact: Any) -> bool:
    return bool(
        artifact is not None
        and artifact.media_type == MCP_APP_MIME_TYPE
        and (artifact.artifact_metadata or {}).get("bridgeVersion") == 2
    )


def _panel_open_tool(artifact: Any, *, supports_apps: bool) -> Tool:
    meta = None
    if supports_apps:
        meta = {
            "ui": {
                "resourceUri": _resource_uri(artifact.id),
                "visibility": ["model"],
            }
        }
    return Tool(
        name=f"{PANEL_TOOL_PREFIX}{artifact.id}",
        title=artifact.name,
        description=f"Open the generated panel '{artifact.name}'.",
        input_schema={"type": "object", "additionalProperties": False},
        annotations=ToolAnnotations(
            # Opening a panel mints a launch grant, which writes a row.
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        _meta=meta,
    )


def _panel_call_tool() -> Tool:
    return Tool(
        name=PANEL_CALL_TOOL,
        title="Operate an opened panel",
        description="Execute a capability-bound operation from an opened WoTBot panel.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "grant": {"type": "string"},
                "tool": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["grant", "tool", "arguments"],
        },
        _meta={"ui": {"visibility": ["app"]}},
    )


def _panel_resource(artifact: Any) -> Resource:
    metadata = artifact.artifact_metadata or {}
    return Resource(
        uri=_resource_uri(artifact.id),
        name=f"panel-{artifact.id}",
        title=artifact.name,
        description=f"Generated MCP App panel '{artifact.name}'.",
        mime_type=MCP_APP_MIME_TYPE,
        # `ui` is the cached listing hint; reads recompute it with the document.
        _meta={"ui": metadata.get("ui") or {}},
    )


def _tool_error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )


def _resource_uri(artifact_id: str) -> str:
    return f"{PANEL_RESOURCE_PREFIX}{artifact_id}"


def _artifact_id_from_tool(tool_name: str) -> str:
    return _validated_artifact_id(tool_name.removeprefix(PANEL_TOOL_PREFIX))


def _artifact_id_from_resource(uri: str) -> str:
    value = str(uri)
    if not value.startswith(PANEL_RESOURCE_PREFIX):
        raise MCPError(INVALID_PARAMS, "Resource not found")
    return _validated_artifact_id(value.removeprefix(PANEL_RESOURCE_PREFIX))


def _validated_artifact_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise MCPError(INVALID_PARAMS, "Invalid panel identifier") from exc
    canonical = str(parsed)
    if value != canonical:
        raise MCPError(INVALID_PARAMS, "Invalid panel identifier")
    return canonical


def _encode_cursor(created_at: datetime, artifact_id: str) -> str:
    raw = json.dumps(
        {"createdAt": created_at.isoformat(), "id": artifact_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        created_at = datetime.fromisoformat(value["createdAt"])
        artifact_id = _validated_artifact_id(value["id"])
        if created_at.tzinfo is None:
            raise ValueError("timezone required")
        return created_at, artifact_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise MCPError(INVALID_PARAMS, "Invalid pagination cursor") from exc


def _next_cursor(items: list[Any], has_more: bool) -> str | None:
    if not has_more or not items:
        return None
    last = items[-1]
    return _encode_cursor(last.created_at, last.id)
