"""FastAPI entrypoint for WoTBot."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI

try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
except ImportError:  # pragma: no cover - exercised when optional dep is absent locally.
    AsyncPostgresSaver = None  # type: ignore[assignment]

from wotbot.agent import build_graph
from wotbot.agent.tools import LOCAL_TOOLS, REGISTRY_TOOLS
from wotbot.api.wot_runtime import router as wot_runtime_router
from wotbot.api_keys.router import router as api_keys_router
from wotbot.auth.router import router as me_router
from wotbot.catalog.router import router as things_router
from wotbot.core.api_dependencies import verify_internal_api_key
from wotbot.core.config import get_settings as get_registry_settings
from wotbot.core.config_router import router as config_router
from wotbot.core.database import get_connection_pool, init_db, psycopg_conninfo
from wotbot.core.health import router as registry_health_router
from wotbot.core.lifecycle import shutdown_backend_runtime, start_backend_runtime
from wotbot.core.llm import make_llm
from wotbot.core.settings import Settings as AgentSettings
from wotbot.discovery.routes import router as discovery_router
from wotbot.jobs.active import set_active_job_service
from wotbot.jobs.routes import router as jobs_router
from wotbot.jobs.service import JobService
from wotbot.media.routes import create_media_router
from wotbot.panels.router import router as panels_router
from wotbot.search.router import router as search_router
from wotbot.threads.routes import create_threads_router
from wotbot.threads.runs import RunRegistry
from wotbot.virtual_things.routes import router as virtual_things_router

logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _checkpoint_database_url(
    *,
    settings: AgentSettings,
    registry_database_url: str,
) -> str:
    if settings.agent_state_database_url:
        return settings.agent_state_database_url

    return registry_database_url


def configure_logging(log_level: str) -> None:
    """Configure process logging even when uvicorn installed handlers first."""
    logging.basicConfig(level=log_level, format=LOG_FORMAT, force=True)
    for logger_name in (
        "wotbot",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        # Surface the LLM/provider call path so upstream errors (e.g. a
        # context-length 4xx from OpenRouter) are visible at DEBUG instead of
        # vanishing into an abrupt stream close.
        "httpx",
        "openai",
        "langgraph",
        "langchain",
    ):
        logging.getLogger(logger_name).setLevel(log_level)


@asynccontextmanager
async def _checkpoint_saver_context(
    *,
    settings: AgentSettings,
    registry_database_url: str,
):
    database_url = _checkpoint_database_url(
        settings=settings,
        registry_database_url=registry_database_url,
    )
    if AsyncPostgresSaver is None:
        raise RuntimeError(
            "Postgres checkpointing requires langgraph-checkpoint-postgres to be installed"
        )

    async with AsyncPostgresSaver.from_conn_string(
        psycopg_conninfo(database_url)
    ) as postgres_saver:
        await postgres_saver.setup()
        yield postgres_saver


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AgentSettings()
    app.state.agent_settings = settings
    app.state.checkpointer = None
    app.state.graph = None
    configure_logging(settings.log_level)
    await asyncio.to_thread(init_db)

    registry_settings = get_registry_settings()
    connection_pool = get_connection_pool()
    await start_backend_runtime(
        app,
        settings=registry_settings,
        connection_pool=connection_pool,
    )

    try:
        llm = make_llm(settings)

        async with _checkpoint_saver_context(
            settings=settings,
            registry_database_url=registry_settings.DATABASE_URL,
        ) as saver:
            logger.info("Using LangGraph Postgres saver for checkpoints")
            checkpointer = saver
            app.state.checkpointer = checkpointer
            reasoning_effort = settings.reasoning_effort
            graph = build_graph(
                llm=llm,
                registry_tools=REGISTRY_TOOLS,
                local_tools=LOCAL_TOOLS,
                max_tokens=settings.max_context_tokens,
                checkpointer=checkpointer,
                parallel_tool_calls=settings.parallel_tool_calls,
                # Inert in this process: the frame registry is per-process
                # in-memory state written only by the LiveKit agent worker, so
                # the lookup here always comes back empty. Kept in step with the
                # worker's graph so the two stay identical if the registry ever
                # becomes shared.
                camera_frames_enabled=settings.openai_model_supports_vision,
                handoff_enabled=settings.agent_handoff_enabled,
                reasoning_effort=reasoning_effort if reasoning_effort.enabled else None,
            )
            graph = graph.with_config(recursion_limit=settings.recursion_limit)
            app.state.graph = graph

            logger.info(
                "Graph created with %d registry tools, model=%s, recursion_limit=%d",
                len(REGISTRY_TOOLS),
                settings.openai_model,
                settings.recursion_limit,
            )

            app.state.settings = settings
            app.state.agent_settings = settings
            job_service = JobService(settings)
            await job_service.start()
            app.state.service = job_service
            set_active_job_service(job_service)
            logger.info("Job API started")

            try:
                async with AsyncExitStack() as stack:
                    if settings.a2a_enabled or settings.mcp_enabled:
                        from wotbot.a2a.runtime import A2ARuntime
                        from wotbot.agent_api.runtime import AgentRuntime
                        from wotbot.agent_api.raw import build_raw_graph
                        from wotbot.agent_api.subscriptions import RawSubscriptions

                        subscriptions = RawSubscriptions(settings)
                        raw_graph = build_raw_graph(checkpointer, subscriptions=subscriptions)
                        runtime = AgentRuntime(
                            graph=graph,
                            raw_graph=raw_graph,
                            subscriptions=subscriptions,
                            registry=run_registry,
                            settings=settings,
                        )
                        app.state.agent_runtime = runtime
                        app.state.a2a_runtime = A2ARuntime(service=runtime)
                        await runtime.start()
                        stack.push_async_callback(runtime.close)
                        for mcp_runtime in mcp_runtimes:
                            await stack.enter_async_context(mcp_runtime.lifespan())
                    yield
            finally:
                app.state.a2a_runtime = None
                app.state.agent_runtime = None
                set_active_job_service(None)
                await job_service.stop()
                app.state.checkpointer = None
                app.state.graph = None
    finally:
        app.state.agent_settings = None
        await shutdown_backend_runtime(app)


app = FastAPI(title="WoTBot", lifespan=lifespan)
app.include_router(registry_health_router)
app.include_router(config_router)
app.include_router(me_router)
app.include_router(search_router)
app.include_router(things_router)
app.include_router(api_keys_router)
app.include_router(jobs_router)
app.include_router(wot_runtime_router)
app.include_router(discovery_router)
app.include_router(panels_router)
app.include_router(virtual_things_router)


def _current_settings() -> AgentSettings | None:
    return getattr(app.state, "agent_settings", None)


def _current_checkpointer() -> Any | None:
    return getattr(app.state, "checkpointer", None)


def _current_graph() -> Any | None:
    # The compiled graph, already carrying recursion_limit via with_config().
    return getattr(app.state, "graph", None)


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(
    create_media_router(
        get_settings=_current_settings,
        verify_internal_api_key=verify_internal_api_key,
    )
)
# One registry for every entry point that can start a graph run (chat UI and
# the A2A router), so two callers cannot run the same thread concurrently.
run_registry = RunRegistry()
app.state.run_registry = run_registry
app.include_router(
    create_threads_router(
        get_checkpointer=_current_checkpointer,
        verify_internal_api_key=verify_internal_api_key,
        get_graph=_current_graph,
        get_settings=_current_settings,
        run_registry=run_registry,
    )
)

surface_settings = AgentSettings()
mcp_runtimes = []
if surface_settings.a2a_enabled or surface_settings.mcp_enabled:
    from wotbot.a2a.server import install_a2a
    from wotbot.agent_api.http import install_downloads
    from wotbot.mcp_apps.assets import router as mcp_assets_router
    from wotbot.mcp_apps.server import MCPPanelRuntime

    install_downloads(app, surface_settings)
    if surface_settings.a2a_enabled:
        install_a2a(app, surface_settings)
    mcp_runtimes.append(MCPPanelRuntime(settings=surface_settings))
    if surface_settings.mcp_enabled:
        from wotbot.mcp.server import MCPToolRuntime

        for profile in ("assistant", "intents", "raw"):
            mcp_runtimes.append(
                MCPToolRuntime(
                    profile=profile,
                    settings=surface_settings,
                    get_runtime=lambda: getattr(app.state, "agent_runtime", None),
                )
            )
    app.include_router(mcp_assets_router)
    for mcp_runtime in mcp_runtimes:
        mcp_runtime.install(app)
