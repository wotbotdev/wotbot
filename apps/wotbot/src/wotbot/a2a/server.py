"""Official A2A 1.0 HTTP+JSON routes and API-key authentication."""

import asyncio
import logging
from contextlib import aclosing

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
from starlette.routing import Route

from wotbot.a2a.downloads import ArtifactDownloadLinks
from wotbot.agent.intents import INTENTS, OUTPUT_MODES
from wotbot.agent_api.http import install_downloads as install_downloads

logger = logging.getLogger(__name__)


def build_agent_card(settings) -> AgentCard:
    return AgentCard(
        name="WoTBot",
        description=(
            "Delegate work with connected devices, APIs and datasets to WoTBot: discover and "
            "onboard resources, operate registered Things, analyze data, create automations "
            "and virtual Things, or generate panels. Send text or JSON instructions; WoTBot "
            "selects the capabilities needed for the request. Results arrive as tasks with "
            "messages and artifacts. Reuse messageId only for identical retries and contextId "
            "for later requests in the same conversation. Paused tasks describe the input or "
            "credential setup needed to continue; resume them using taskId and the requested "
            "reply schemas. Files have temporary download links; retrieve the task again to "
            "refresh a link while the file is retained. Panels provide a panelUrl that opens "
            "the WoTBot UI with its existing access controls."
        ),
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
                id=intent.id,
                name=intent.name,
                description=intent.description,
                examples=[intent.example],
                tags=[intent.id],
                input_modes=["text/plain", "application/json"],
                output_modes=list(intent.outputs),
            )
            for intent in INTENTS.values()
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
    install_downloads(app, settings)
