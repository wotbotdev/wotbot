"""Authenticated MCP resource-server plumbing shared by the tool profiles.

Transport, API-key auth, DNS-rebinding/CORS policy and mounting live here; the
tool, resource and prompt surface belongs to the concrete runtime subclass.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ListResourcesResult, TextContent
from pydantic import AnyHttpUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wotbot.agent_api.artifacts import ArtifactStore
from wotbot.auth.models import User
from wotbot.auth.providers import get_api_key_user_from_token
from wotbot.clients.runtime_stream import close_runtime_stream_clients
from wotbot.core.settings import Settings


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


class MCPServerRuntime:
    def __init__(
        self,
        *,
        path,
        title,
        instructions,
        settings=None,
        artifact_store=None,
    ):
        self.path = path
        self.settings = settings or Settings()
        self.artifact_store = artifact_store or ArtifactStore()
        self.server = Server(
            "wotbot-" + path.rsplit("/", 1)[-1],
            version="1.0.0",
            title=title,
            instructions=instructions,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_read_resource=self._read_resource,
        )
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
                "Mcp-Method",
                "Mcp-Name",
                "Last-Event-ID",
            ],
            expose_headers=["Mcp-Session-Id"],
        )
        self.root_endpoint = MCPRootEndpoint(self.asgi_app, path=self.path)

    def install(self, app):
        # Preflight must reach this app's CORS middleware at the exact URL,
        # rather than receiving a parent-router 405 or a slash redirect.
        app.router.routes.append(
            Route(
                self.path,
                self.root_endpoint,
                methods=["GET", "POST", "DELETE", "OPTIONS"],
            )
        )
        app.mount(self.path, self.asgi_app)

    @asynccontextmanager
    async def lifespan(self):
        async with self.asgi_app.router.lifespan_context(self.asgi_app):
            try:
                yield
            finally:
                await close_runtime_stream_clients()

    async def _list_tools(self, ctx, params):
        raise NotImplementedError

    async def _call_tool(self, ctx, params):
        raise NotImplementedError

    async def _list_resources(self, ctx, params):
        # Artifacts are discovered through tool results and artifact.list. The
        # SDK still needs this handler to advertise resources/read support.
        return ListResourcesResult(resources=[], cache_scope="private", ttl_ms=0)

    async def _read_resource(self, ctx, params):
        raise MCPError(INVALID_PARAMS, "Resource not found")


def request_user() -> User:
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


def tool_error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )
