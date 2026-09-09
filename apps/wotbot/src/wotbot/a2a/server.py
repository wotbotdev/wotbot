"""Official A2A 1.0 HTTP+JSON routes and API-key authentication."""

import asyncio
import logging
import re
from contextlib import aclosing
from urllib.parse import quote, urlencode

from a2a.auth.user import User as A2AUser
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler, validate_request_params
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    ListTasksResponse,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from wotbot.a2a.downloads import ArtifactDownloadLinks, InvalidDownloadLink
from wotbot.auth.providers import get_api_key_user
from wotbot.core.time import utc_now

logger = logging.getLogger(__name__)

OUTPUT_MODES = [
    "text/plain",
    "application/json",
    "image/png",
    "image/jpeg",
    "application/vnd.plotly.v1+json",
    "application/octet-stream",
]
SKILLS = [
    (
        "chat",
        "Conversation",
        "Answer questions about Things and smart living.",
        "What can you help me with?",
    ),
    (
        "control",
        "Device control",
        "Read and operate registered Things and create control panels.",
        "Turn off the living room lights.",
    ),
    (
        "analysis",
        "Analysis",
        "Analyze Thing data and generate charts, files and dashboards.",
        "Analyze yesterday's energy use and create a dashboard.",
    ),
    (
        "jobs",
        "Automation jobs",
        "Create and manage scheduled or event-driven automations.",
        "Schedule the lights to turn off at 11 pm.",
    ),
    (
        "virtual_things",
        "Virtual Things",
        "Create and manage virtual Things backed by existing capabilities.",
        "Create a virtual Thing for total home power.",
    ),
    (
        "discovery",
        "Discovery",
        "Find Things and external sources and inspect their capabilities.",
        "Find temperature sensors and show their properties.",
    ),
]


def build_agent_card(settings) -> AgentCard:
    return AgentCard(
        name="WoTBot",
        description="Web of Things assistant for discovery, control, analysis, automation and generated panels. Requests are routed automatically.",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=settings.registry_public_url.rstrip("/") + "/a2a/v1",
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="WoTBot API key",
                    description="API key with agent:invoke (full assistant delegation)",
                )
            )
        },
        security_requirements=[
            SecurityRequirement(schemes={"bearer": StringList(list=["agent:invoke"])})
        ],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=OUTPUT_MODES,
        skills=[
            AgentSkill(
                id=id,
                name=name,
                description=description,
                examples=[example],
                tags=[id],
                input_modes=["text/plain", "application/json"],
                output_modes=OUTPUT_MODES
                if id == "analysis"
                else ["text/plain", "application/json"],
            )
            for id, name, description, example in SKILLS
        ],
    )


class AuthenticatedUser(A2AUser):
    def __init__(self, principal):
        self.principal = principal

    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return self.principal.api_key_id


class CallContextBuilder(ServerCallContextBuilder):
    def build(self, request):
        user = request.state.a2a_user
        return ServerCallContext(
            user=AuthenticatedUser(user),
            state={"principal": user, "headers": dict(request.headers)},
        )


class A2AAuthMiddleware:
    """ASGI middleware: a disconnected stream does not cancel the independent runner."""

    def __init__(self, app, *, download_links):
        self.app = app
        self.download_links = download_links

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/a2a/"):
            request = Request(scope)
            tokens = request.query_params.getlist("downloadToken")
            if tokens:
                # Resolve the capability here, then remove it from the scope
                # used by Uvicorn access logs and downstream request handling.
                scope["query_string"] = urlencode(
                    [
                        (key, value)
                        for key, value in request.query_params.multi_items()
                        if key != "downloadToken"
                    ]
                ).encode()
            download = re.fullmatch(r"/a2a/artifacts/([^/]+)", scope["path"])
            if (
                tokens
                and download
                and scope["method"] in {"GET", "HEAD"}
                and not request.headers.get("Authorization")
            ):
                try:
                    if len(tokens) != 1:
                        raise InvalidDownloadLink()
                    user = await self.download_links.authorize(tokens[0], download[1])
                except InvalidDownloadLink:
                    return await JSONResponse(
                        {
                            "error": "Download link is invalid or expired. Retrieve the A2A task again with your API key for a fresh link."
                        },
                        status_code=410,
                        headers={
                            "Cache-Control": "private, no-store",
                            "Referrer-Policy": "no-referrer",
                        },
                    )(scope, receive, send)
            else:
                user = await asyncio.to_thread(get_api_key_user, request)
            if user is None or not user.api_key_id:
                response = JSONResponse(
                    {
                        "error": {
                            "code": 401,
                            "status": "UNAUTHENTICATED",
                            "message": "A valid API key is required",
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return await response(scope, receive, send)
            if "agent:invoke" not in (user.scopes or []):
                return await JSONResponse(
                    {
                        "error": {
                            "code": 403,
                            "status": "PERMISSION_DENIED",
                            "message": "Missing scope: agent:invoke",
                        }
                    },
                    status_code=403,
                )(scope, receive, send)
            scope.setdefault("state", {})["a2a_user"] = user
        await self.app(scope, receive, send)


def _reject_tenant(params):
    """Every request carries a tenant field; none of them are honoured.

    Rejecting it on send but ignoring it everywhere else would let a caller
    believe reads were scoped to a tenant when they never are.
    """
    if params.tenant:
        raise InvalidParamsError("Tenants are not supported")


def _history(task, params, *, include_artifacts=True):
    if params.history_length < 0:
        raise InvalidParamsError("historyLength must be nonnegative")
    if params.HasField("history_length"):
        history = list(task.history)[-params.history_length :] if params.history_length else []
        del task.history[:]
        task.history.extend(history)
    if not include_artifacts:
        del task.artifacts[:]
    return task


class WoTBotRequestHandler(RequestHandler):
    def __init__(self, get_runtime, download_links):
        self.get_runtime = get_runtime
        self.download_links = download_links

    @property
    def runtime(self):
        runtime = self.get_runtime()
        if runtime is None:
            raise UnsupportedOperationError("Assistant is not ready")
        return runtime

    @validate_request_params
    async def on_message_send(self, params, context):
        owner = context.user.user_name
        # Admission includes committing retry identity and launching its runner.
        # A disconnect between those steps must not strand a submitted task.
        admission = await asyncio.shield(self.runtime.admit(owner, params))
        if not params.configuration.return_immediately:
            runner = self.runtime.runners.get(admission.task.id)
            if runner:
                await asyncio.shield(runner)
        task = await asyncio.to_thread(self.runtime.store.get, owner, admission.task.id)
        return await self.download_links.present(owner, _history(task, params.configuration))

    @validate_request_params
    async def on_message_send_stream(self, params, context):
        admission = await asyncio.shield(self.runtime.admit(context.user.user_name, params))
        async with aclosing(
            self.runtime.subscribe(
                context.user.user_name,
                admission.task.id,
                history_length=params.configuration.history_length
                if params.configuration.HasField("history_length")
                else None,
            )
        ) as events:
            async for event in events:
                yield await self.download_links.present(context.user.user_name, event)

    @validate_request_params
    async def on_get_task(self, params, context):
        _reject_tenant(params)
        task = _history(
            await asyncio.to_thread(self.runtime.store.get, context.user.user_name, params.id),
            params,
        )
        return await self.download_links.present(context.user.user_name, task)

    @validate_request_params
    async def on_list_tasks(self, params, context):
        _reject_tenant(params)
        tasks, total, cursor = await asyncio.to_thread(
            self.runtime.store.list, context.user.user_name, params
        )
        return ListTasksResponse(
            tasks=[
                await self.download_links.present(
                    context.user.user_name,
                    _history(t, params, include_artifacts=params.include_artifacts),
                )
                for t in tasks
            ],
            next_page_token=cursor,
            total_size=total,
            page_size=min(params.page_size or 50, 100),
        )

    @validate_request_params
    async def on_cancel_task(self, params, context):
        _reject_tenant(params)
        task = await asyncio.shield(self.runtime.cancel(context.user.user_name, params.id))
        return await self.download_links.present(context.user.user_name, task)

    @validate_request_params
    async def on_subscribe_to_task(self, params, context):
        _reject_tenant(params)
        async with aclosing(
            self.runtime.subscribe(context.user.user_name, params.id, terminal_error=True)
        ) as events:
            async for event in events:
                yield await self.download_links.present(context.user.user_name, event)

    async def on_get_extended_agent_card(self, params, context):
        raise UnsupportedOperationError()

    async def on_create_task_push_notification_config(self, params, context):
        raise UnsupportedOperationError("Push notifications are not supported")

    on_get_task_push_notification_config = on_create_task_push_notification_config
    on_list_task_push_notification_configs = on_create_task_push_notification_config
    on_delete_task_push_notification_config = on_create_task_push_notification_config


def install_a2a(app, settings):
    from wotbot.a2a.artifacts import ArtifactStore

    download_links = ArtifactDownloadLinks(settings)
    handler = WoTBotRequestHandler(lambda: getattr(app.state, "a2a_runtime", None), download_links)
    routes = create_rest_routes(handler, CallContextBuilder(), path_prefix="/a2a/v1")
    app.router.routes.extend(
        r
        for r in routes
        if isinstance(r, Route)
        and "pushNotification" not in r.path
        and "extendedAgentCard" not in r.path
    )
    app.router.routes.extend(create_agent_card_routes(build_agent_card(settings)))
    app.add_middleware(A2AAuthMiddleware, download_links=download_links)

    @app.api_route("/a2a/artifacts/{artifact_id}", methods=["GET", "HEAD"], include_in_schema=False)
    async def download(artifact_id: str, request: Request):
        """Stream an export straight from the code executor.

        New bytes stay in the executor. Copies retained by the original A2A
        implementation remain downloadable until their original expiry.
        """
        from wotbot.a2a.outputs import executor_headers

        record = await ArtifactStore().get(
            artifact_id, owner=request.state.a2a_user.api_key_id, include_content=True
        )
        if record is None or record.panel_version_id:
            return _artifact_error("Artifact not found", 404)
        if record.expires_at and record.expires_at <= utc_now():
            return _artifact_error("Artifact has expired", 410)

        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(record.name, safe=""),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        if record.legacy_content is not None:
            headers["Content-Length"] = str(len(record.legacy_content))
            return Response(
                b"" if request.method == "HEAD" else record.legacy_content,
                media_type=record.media_type,
                headers=headers,
            )
        if not record.executor_artifact_id:
            return _artifact_error("Artifact not found", 404)

        url = (
            f"{settings.code_executor_url.rstrip('/')}"
            f"/artifacts/{quote(record.executor_artifact_id, safe='')}/content"
        )
        client = httpx.AsyncClient(timeout=settings.code_executor_timeout_seconds)
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers=executor_headers(settings)),
                stream=True,
            )
        except httpx.HTTPError:
            await client.aclose()
            logger.exception("Could not reach the code executor for artifact=%s", artifact_id)
            return _artifact_error("Artifact is temporarily unavailable", 503)
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            # The executor's own retention swept it before this link expired.
            return _artifact_error(
                "Artifact has expired"
                if upstream.status_code == 404
                else "Artifact is unavailable",
                410 if upstream.status_code == 404 else 502,
            )

        if "content-length" in upstream.headers:
            headers["Content-Length"] = upstream.headers["content-length"]

        async def close():
            await upstream.aclose()
            await client.aclose()

        if request.method == "HEAD":
            await close()
            return Response(status_code=200, media_type=record.media_type, headers=headers)
        return StreamingResponse(
            upstream.aiter_bytes(),
            media_type=record.media_type,
            headers=headers,
            background=BackgroundTask(close),
        )


def _artifact_error(message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": message},
        status_code=status,
        headers={"Cache-Control": "private, no-store"},
    )
