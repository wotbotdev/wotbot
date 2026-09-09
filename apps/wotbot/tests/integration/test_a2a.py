"""Real database + graph checkpoint tests for the inbound A2A adapter."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, TypedDict
from uuid import uuid4

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.types import SendMessageRequest, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from sqlalchemy import text

from wotbot.a2a.models import A2AArtifactRecord, A2ATaskRecord
from wotbot.a2a.runtime import A2ARuntime
from wotbot.a2a.server import build_agent_card, install_a2a
from wotbot.a2a.store import TaskStore
from wotbot.api_keys.models import ApiKey
from wotbot.api_keys.store import create_api_key
from wotbot.core.agent_runs import RunRegistry
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.core.time import utc_now
from wotbot.threads.store import ThreadStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def environment(jobs_integration_environment):
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE threads, a2a_artifacts, api_keys, panels CASCADE"))
        session.commit()
        first, token = create_api_key(
            session, user_id="same-admin", name="first", scopes=["agent:invoke"]
        )
        second, token2 = create_api_key(
            session, user_id="same-admin", name="second", scopes=["agent:invoke"]
        )
        limited, token3 = create_api_key(
            session, user_id="same-admin", name="limited", scopes=["things:read"]
        )
    return first.id, token, second.id, token2, token3


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def graph_for(node):
    graph = StateGraph(State)
    graph.add_node("respond", node)
    graph.add_edge(START, "respond")
    return graph.compile(checkpointer=InMemorySaver())


def request(text="Hello", *, id=None, context=None, task=None, data=None):
    message = {
        "messageId": id or str(uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}] if data is None else [{"data": data}],
    }
    if context:
        message["contextId"] = context
    if task:
        message["taskId"] = task
    return ParseDict({"message": message}, SendMessageRequest())


@asynccontextmanager
async def running(graph, *, settings=None, **kwargs):
    runtime = A2ARuntime(
        graph=graph, registry=RunRegistry(), settings=settings or Settings(), **kwargs
    )
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()


@asynccontextmanager
async def fake_executor(files):
    """Serve the two ``/artifacts`` endpoints A2A relies on.

    Exports are no longer copied into Postgres, so the download path is only
    meaningful against something that actually streams bytes back.
    """
    import uvicorn
    from fastapi import HTTPException
    from starlette.responses import Response

    app = FastAPI()

    def described(artifact_id):
        filename, content, mime = files[artifact_id]
        return {
            "id": artifact_id,
            "filename": filename,
            "mime_type": mime,
            "size_bytes": len(content),
            "sha256": "0" * 64,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        }

    @app.get("/artifacts/{artifact_id}/metadata")
    async def metadata(artifact_id: str):
        if artifact_id not in files:
            raise HTTPException(status_code=404)
        return described(artifact_id)

    @app.get("/artifacts/{artifact_id}/content")
    async def content(artifact_id: str):
        if artifact_id not in files:
            raise HTTPException(status_code=404)
        _, body, mime = files[artifact_id]
        return Response(body, media_type=mime)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await serving


async def finish(runtime, task):
    runner = runtime.runners.get(task.id)
    if runner:
        await asyncio.wait_for(asyncio.shield(runner), 5)
    return runtime.store.get(runtime.owner, task.id)


async def test_sdk_http_binding_skills_and_key_isolation(environment):
    owner, token, other, token2, limited = environment
    from wotbot.agent.builder import build_graph
    from wotbot.agent.nodes import IntentClassification

    class Model:
        intent = "chat"

        def with_structured_output(self, schema):
            class Classifier:
                async def ainvoke(_, *args, **kwargs):
                    return IntentClassification(intent=self.intent)

            return Classifier()

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            assert any("external agent over A2A" in str(m.content) for m in messages)
            return AIMessage(content=f"{self.intent} completed")

    from wotbot.agent.tools import LOCAL_TOOLS

    model = Model()
    graph = build_graph(
        llm=model,
        registry_tools=[],
        local_tools=LOCAL_TOOLS,
        max_tokens=2000,
        checkpointer=InMemorySaver(),
    )
    async with running(graph) as runtime:
        app = FastAPI()
        app.state.a2a_runtime = runtime
        install_a2a(app, Settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        ) as http:
            card = await http.get("/.well-known/agent-card.json", headers={"Authorization": ""})
            assert card.status_code == 200
            assert {s["id"] for s in card.json()["skills"]} == {
                "chat",
                "control",
                "analysis",
                "jobs",
                "virtual_things",
                "discovery",
            }
            client = ClientFactory(
                ClientConfig(
                    streaming=False, httpx_client=http, supported_protocol_bindings=["HTTP+JSON"]
                )
            ).create(build_agent_card(Settings()))
            for skill in card.json()["skills"]:
                model.intent = skill["id"]
                events = [e async for e in client.send_message(request(skill["examples"][0]))]
                assert events
                task = events[-1].task
                assert task.status.state == TaskState.TASK_STATE_COMPLETED
                assert task.history[-1].parts[0].text == f"{skill['id']} completed"
            assert ThreadStore().list() == []
            hidden = ThreadStore().list(include_hidden=True)
            assert len(hidden) == 6 and all(t["kind"] == "a2a" and not t["visible"] for t in hidden)
            result = await http.get(f"/a2a/v1/tasks/{task.id}")
            assert result.status_code == 200
            assert (
                await http.get(
                    f"/a2a/v1/tasks/{task.id}", headers={"Authorization": f"Bearer {token2}"}
                )
            ).status_code == 404
            assert (
                await http.get("/a2a/v1/tasks", headers={"Authorization": f"Bearer {token2}"})
            ).json().get("tasks", []) == []
            assert (
                await http.get("/a2a/v1/tasks", headers={"Authorization": f"Bearer {limited}"})
            ).status_code == 403
            assert (
                await http.get("/a2a/v1/tasks", headers={"Authorization": ""})
            ).status_code == 401
            assert (await http.post("/api/a2a/message", json={})).status_code == 404
            from a2a.utils.errors import ContentTypeNotSupportedError

            unsupported = request()
            unsupported.message.parts[0].media_type = "text/html"
            with pytest.raises(ContentTypeNotSupportedError):
                await runtime.admit(owner, unsupported)
            unsupported.message.parts[0].raw = b"binary upload"
            with pytest.raises(ContentTypeNotSupportedError):
                await runtime.admit(owner, unsupported)
            with pytest.raises(InvalidParamsError):
                await runtime.admit(other, request(context=task.context_id))
            with pytest.raises(InvalidParamsError):
                await runtime.admit(owner, request(context=str(uuid4())))
            # The context IDs handed out are exactly the hidden thread IDs.
            assert task.context_id in {t["id"] for t in hidden}


async def test_dedup_concurrency_disconnect_reconnect_and_followup(environment):
    owner = environment[0]
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def node(state):
        effects.append("action")
        entered.set()
        await release.wait()
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        original = request(id="retry-me")
        a, b = await asyncio.gather(runtime.admit(owner, original), runtime.admit(owner, original))
        assert a.task.id == b.task.id
        await entered.wait()
        with pytest.raises(InvalidParamsError):
            await runtime.admit(owner, request("changed", id="retry-me"))
        with pytest.raises(InvalidParamsError):
            await runtime.admit(owner, request(context=a.task.context_id))
        stream = runtime.subscribe(owner, a.task.id)
        assert (await anext(stream)).id == a.task.id
        await stream.aclose()
        assert not runtime.runners[a.task.id].done()
        reconnect = runtime.subscribe(owner, a.task.id)
        await anext(reconnect)
        release.set()
        events = [e async for e in reconnect]
        assert events[-1].status.state == TaskState.TASK_STATE_COMPLETED
        assert effects == ["action"]
        retry = await runtime.admit(owner, original)
        assert not retry.execute
        followup = await runtime.admit(owner, request(context=a.task.context_id))
        await finish(runtime, followup.task)
        assert effects == ["action", "action"]
        with pytest.raises(InvalidParamsError):
            await runtime.admit(owner, request(context=followup.task.context_id, task=a.task.id))


@pytest.mark.parametrize(
    "kind,response,expected",
    [
        ("credential", {"status": "credential_saved"}, TaskState.TASK_STATE_AUTH_REQUIRED),
        ("confirmation", {"approved": True}, TaskState.TASK_STATE_INPUT_REQUIRED),
        (
            "source_registration",
            {"status": "source_registered", "source_id": "source"},
            TaskState.TASK_STATE_INPUT_REQUIRED,
        ),
        ("input", "answer", TaskState.TASK_STATE_INPUT_REQUIRED),
    ],
)
async def test_pauses_survive_restart_and_only_validated_replies_resume(
    environment, kind, response, expected
):
    owner = environment[0]
    resumed = []

    async def node(state):
        answer = interrupt({"kind": kind, "question": "Please respond", "thread_id": "internal"})
        resumed.append(answer)
        return {"messages": [AIMessage(content="Resumed")]}

    graph = graph_for(node)
    async with running(graph) as runtime:
        runtime.owner = owner
        admission = await runtime.admit(owner, request())
        paused = await finish(runtime, admission.task)
        assert paused.status.state == expected
    async with running(graph) as runtime:
        runtime.owner = owner
        saved = runtime.store.get(owner, paused.id)
        assert saved.status.state == expected
        pending = MessageToDict(saved.status.message.parts[-1])["data"]["requests"][0]
        assert "thread_id" not in pending["details"]
        with pytest.raises(InvalidParamsError):
            await runtime.admit(owner, request(context=paused.context_id))
        with pytest.raises(InvalidParamsError):
            await runtime.admit(
                owner, request(task=paused.id, data={"goto": "tools", "resume": True})
            )
        if kind == "credential":
            with pytest.raises(InvalidParamsError):
                await runtime.admit(
                    owner,
                    request(
                        task=paused.id,
                        data={
                            "requestId": pending["requestId"],
                            "response": {"status": "credential_saved", "apiKey": "secret"},
                        },
                    ),
                )
        resumed_request = request(
            task=paused.id,
            context=paused.context_id,
            data={"requestId": pending["requestId"], "response": response},
        )
        continuation = await runtime.admit(owner, resumed_request)
        assert continuation.task.id == paused.id
        result = await finish(runtime, continuation.task)
        assert result.status.state == TaskState.TASK_STATE_COMPLETED
        assert resumed == [response]
        assert not (await runtime.admit(owner, resumed_request)).execute


async def test_cancellation_finishes_checkpoint_cleanup(environment):
    owner = environment[0]
    entered = asyncio.Event()

    async def node(state):
        entered.set()
        await asyncio.Event().wait()

    graph = graph_for(node)
    async with running(graph) as runtime:
        a = await runtime.admit(owner, request())
        await entered.wait()
        task = await runtime.cancel(owner, a.task.id)
        assert task.status.state == TaskState.TASK_STATE_CANCELED
        assert not runtime.registry.is_running(a.thread_id)
        snapshot = await graph.aget_state({"configurable": {"thread_id": a.thread_id}})
        assert isinstance(snapshot.values["messages"][-1], AIMessage)


async def test_restart_marks_abandoned_tasks_failed_without_repeating_actions(environment):
    owner = environment[0]
    store = TaskStore()
    a = store.admit(owner, MessageToDict(request()))
    calls = []

    async def node(state):
        calls.append("action")
        return {"messages": [AIMessage(content="Oops")]}

    async with running(graph_for(node)) as runtime:
        recovered = runtime.store.get(owner, a.task.id)
        assert recovered.status.state == TaskState.TASK_STATE_FAILED
        assert "uncertain" in recovered.status.message.parts[0].text
        assert calls == []


async def test_revocation_stops_running_execution_and_blocks_retrieval(environment):
    owner = environment[0]
    entered = asyncio.Event()

    async def node(state):
        entered.set()
        await asyncio.Event().wait()

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        a = await runtime.admit(owner, request())
        await entered.wait()
        with get_session_factory()() as session:
            session.get(ApiKey, owner).is_active = False
            session.commit()
        task = await finish(runtime, a.task)
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert "revoked" in task.status.message.parts[0].text
        assert not runtime.registry.is_running(a.thread_id)


async def test_artifacts_stream_from_the_executor_and_expire_with_owner_isolation(environment):
    owner, token, other, token2, _ = environment

    async def node(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "code", "name": "run_code", "args": {"code": "plot()"}}],
                ),
                ToolMessage(
                    id="result",
                    tool_call_id="code",
                    content=json.dumps(
                        {
                            "ok": True,
                            "stdout": "Usage: 12",
                            "artifacts": [
                                {"kind": "image", "filename": "chart.png"},
                                {"kind": "plotly", "filename": "chart.json"},
                                {"kind": "file", "id": "export-id", "filename": "usage.csv"},
                            ],
                        }
                    ),
                ),
                AIMessage(content="Energy usage analyzed"),
            ]
        }

    files = {
        "chart.png": ("chart.png", b"\x89PNG", "image/png"),
        "chart.json": ("chart.json", b'{"data":[{"y":[12]}]}', "application/json"),
        "export-id": ("usage.csv", b"usage\n12", "text/csv"),
    }
    async with fake_executor(files) as executor_url:
        settings = Settings(code_executor_url=executor_url)
        async with running(graph_for(node), settings=settings) as runtime:
            runtime.owner = owner
            admission = await runtime.admit(owner, request())
            task = await finish(runtime, admission.task)
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            assert len(task.artifacts) == 4
            assert task.history[-1].parts[0].text == "Energy usage analyzed"
            # A figure still travels inline; it is the one class whose bytes are read.
            assert MessageToDict(task.artifacts[1].parts[1])["data"]["figure"]["data"][0]["y"] == [
                12
            ]
            assert MessageToDict(task.artifacts[3].parts[0])["data"]["kind"] == "wotbot.tool_result"

            # Nothing was copied: the row points at the executor's identifier.
            with get_session_factory()() as session:
                stored = session.get(A2AArtifactRecord, task.artifacts[2].artifact_id)
                assert stored.executor_artifact_id == "export-id"
                assert not hasattr(stored, "content")

            app = FastAPI()
            app.state.a2a_runtime = runtime
            install_a2a(app, settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:8000",
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                artifact = task.artifacts[2]
                url = artifact.parts[0].url
                response = await client.get(url)
                assert response.status_code == 200 and response.content == b"usage\n12"
                assert "usage.csv" in response.headers["content-disposition"]
                assert (await client.head(url)).status_code == 200
                assert (
                    await client.get(url, headers={"Authorization": f"Bearer {token2}"})
                ).status_code == 404
                with get_session_factory()() as session:
                    session.get(A2AArtifactRecord, artifact.artifact_id).expires_at = (
                        utc_now() - timedelta(seconds=1)
                    )
                    session.commit()
                assert (await client.get(url)).status_code == 410
                assert len(runtime.store.get(owner, task.id).artifacts) == 4


async def test_download_reports_410_when_the_executor_has_swept_the_bytes(environment):
    owner, token, *_ = environment

    async def node(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "code", "name": "run_code", "args": {}}],
                ),
                ToolMessage(
                    id="result",
                    tool_call_id="code",
                    content=json.dumps(
                        {"ok": True, "artifacts": [{"kind": "file", "id": "vanishing"}]}
                    ),
                ),
                AIMessage(content="Done"),
            ]
        }

    files = {"vanishing": ("vanishing.csv", b"a,b\n", "text/csv")}
    async with fake_executor(files) as executor_url:
        settings = Settings(code_executor_url=executor_url)
        async with running(graph_for(node), settings=settings) as runtime:
            runtime.owner = owner
            task = await finish(runtime, (await runtime.admit(owner, request())).task)
            app = FastAPI()
            app.state.a2a_runtime = runtime
            install_a2a(app, settings)
            # The executor's own retention can outrun the A2A link's expiry.
            files.clear()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:8000",
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                assert (await client.get(task.artifacts[0].parts[0].url)).status_code == 410


async def test_export_failure_is_explicit_and_invalid_tool_outputs_are_ignored(environment):
    from wotbot.a2a.outputs import ArtifactCollector

    owner = environment[0]

    async def node(state):
        return {
            "messages": [
                AIMessage(content="", tool_calls=[{"id": "code", "name": "run_code", "args": {}}]),
                ToolMessage(
                    id="bad", tool_call_id="not-executed", content='{"artifacts":[{"kind":"web"}]}'
                ),
                ToolMessage(
                    id="good",
                    tool_call_id="code",
                    content='{"artifacts":[{"kind":"file","filename":"gone"}]}',
                ),
                AIMessage(content="Result"),
            ]
        }

    async def fetch(settings, item):
        raise FileNotFoundError()

    async with running(
        graph_for(node),
        collector_factory=lambda **kw: ArtifactCollector(settings=Settings(), fetch=fetch, **kw),
    ) as runtime:
        runtime.owner = owner
        a = await runtime.admit(owner, request())
        task = await finish(runtime, a.task)
        assert task.status.state == TaskState.TASK_STATE_FAILED
        assert "export" in task.status.message.parts[0].text
        assert not task.artifacts


async def test_retention_retires_expired_tasks_and_their_conversations(environment):
    from wotbot.threads.models import Thread

    owner = environment[0]

    async def node(state):
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        a = await runtime.admit(owner, request(id="expiring"))
        await finish(runtime, a.task)
        context_id = a.task.context_id
        # The context is the hidden thread; there is no separate mapping row.
        with get_session_factory()() as session:
            assert session.get(Thread, context_id).owner_api_key_id == owner
        assert (await runtime.graph.aget_state({"configurable": {"thread_id": context_id}})).values[
            "messages"
        ]

        with get_session_factory()() as session:
            session.get(A2ATaskRecord, a.task.id).expires_at = utc_now() - timedelta(seconds=1)
            session.commit()

        await runtime.sweep()
        with pytest.raises(TaskNotFoundError):
            runtime.store.get(owner, a.task.id)
        # Retention now reaches the conversation itself, not just its task row.
        with get_session_factory()() as session:
            assert session.get(Thread, context_id) is None
        assert not (
            await runtime.graph.aget_state({"configurable": {"thread_id": context_id}})
        ).values.get("messages")
        with pytest.raises(InvalidParamsError):
            await runtime.admit(owner, request(id="later", context=context_id))

        # The deduplication window ended with the task, so the same messageId
        # can open a new conversation.
        again = await runtime.admit(owner, request(id="expiring"))
        await finish(runtime, again.task)
        assert again.task.context_id != context_id


async def test_a2a_contexts_are_not_reachable_through_the_chat_routes(environment):
    from wotbot.threads.routes import create_threads_router

    owner = environment[0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def node(state):
        entered.set()
        await release.wait()
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        admitted = await runtime.admit(owner, request())
        await asyncio.wait_for(entered.wait(), 3)
        context_id = admitted.task.context_id
        app = FastAPI()
        app.include_router(
            create_threads_router(
                get_checkpointer=lambda: runtime.graph.checkpointer,
                verify_internal_api_key=lambda request: None,
                get_graph=lambda: runtime.graph,
                get_settings=Settings,
                run_registry=runtime.registry,
            )
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                # Positive control: this must reach the real router, not a
                # doubled /threads prefix whose every request would be 404.
                assert (await client.get("/threads")).status_code == 200
                assert (await client.post("/threads", json={"id": context_id})).status_code == 404
                for method, suffix, body in (
                    ("GET", "", None),
                    ("PATCH", "", {"title": "Changed", "force": True}),
                    ("GET", "/state", None),
                    ("POST", "/runs/stream", {"input": {"messages": []}}),
                    ("POST", "/runs/fork", {"message_id": "any"}),
                    ("POST", "/runs/cancel", None),
                    ("DELETE", "", None),
                ):
                    response = await client.request(
                        method, f"/threads/{context_id}{suffix}", json=body
                    )
                    assert response.status_code == 404
                    assert response.json() == {"detail": "Thread not found"}
                assert runtime.registry.is_running(context_id)
                assert (
                    runtime.store.get(owner, admitted.task.id).status.state
                    == TaskState.TASK_STATE_WORKING
                )
        finally:
            release.set()
        assert (await finish(runtime, admitted.task)).status.state == TaskState.TASK_STATE_COMPLETED


async def test_mcp_apps_panels_immutable_grants_binary_subscriptions_and_deletion(
    environment, monkeypatch
):
    import base64
    from unittest.mock import AsyncMock, patch

    import redis.asyncio as redis

    from wotbot.a2a.artifacts import ArtifactStore
    from wotbot.a2a.outputs import ArtifactCollector
    from wotbot.core.agent_runs import RunEvent
    from wotbot.mcp_apps.server import MCPPanelRuntime
    from wotbot.panels.models import Panel, PanelVersion
    from wotbot.panels.render import wrap_panel_document
    from wotbot.panels.service import PanelService

    owner, token, other, token2, limited = environment
    markup = "<button onclick=\"wot.writeProperty('lamp','power',true)\">On</button>"
    caps = [
        {
            "thingId": "lamp",
            "affordances": ["power", "media", "changed"],
            "ops": [
                "readProperty",
                "writeProperty",
                "invokeAction",
                "observeProperty",
                "subscribeEvent",
            ],
        }
    ]
    collector = ArtifactCollector(
        settings=Settings(), owner=owner, task_id="panel-task", thread_id="hidden"
    )

    async def make_panel(call_id):
        event = RunEvent(
            "values",
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": call_id,
                                "name": "create_web_interface",
                                "args": {"html": markup, "title": "Lamp"},
                            }
                        ],
                    ),
                    ToolMessage(
                        id=call_id,
                        tool_call_id=call_id,
                        content=json.dumps(
                            {
                                "artifacts": [
                                    {
                                        "kind": "web",
                                        "filename": "preview.html",
                                        "capabilities": caps,
                                    }
                                ]
                            }
                        ),
                    ),
                ]
            },
        )
        result = await collector.consume(event, seen=set())
        assert len(result) == 1
        return result[0], MessageToDict(result[0].parts[0])["data"]

    artifact, descriptor = await make_panel("first")
    artifact2, descriptor2 = await make_panel("second")
    with get_session_factory()() as session:
        panel = session.get(Panel, descriptor["panelId"])
        version = session.get(PanelVersion, descriptor["panelVersionId"])
        assert panel.html == version.html == markup
        assert panel.capabilities == version.capabilities == caps
        assert version.source == "initial"
        assert markup in wrap_panel_document(panel.html, panel.title)
    assert descriptor["panelUrl"] == f"http://localhost:3000/panels?panelId={descriptor['panelId']}"

    binary = {"kind": "binary", "contentType": "image/png", "bodyBase64": "AQI=", "sizeBytes": 2}

    def result(value):
        return {"result": {"success": True, "payload": {"data": value}}}

    device = AsyncMock()
    device.read_property.return_value = result(binary)
    device.write_property.return_value = result(True)
    device.invoke_action.return_value = result(binary)
    device.observe_property.return_value = {"subscription_id": "sub-first"}
    device.subscribe_event.return_value = {"subscription_id": "event-first"}
    device.unsubscribe.return_value = {"ok": True}
    monkeypatch.setattr("wotbot.mcp_apps.service.WotRuntimeClient", lambda settings: device)
    mcp = MCPPanelRuntime(settings=Settings())
    app = FastAPI()
    mcp.install(app)

    async with (
        mcp.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        ) as http,
    ):
        preflight = await http.options(
            "/mcp/apps",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Mcp-Session-Id",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert (
            await http.options(
                "/mcp/apps",
                headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
        ).status_code == 400
        counter = 0

        async def rpc(method, params, *, credential=None, headers=None):
            nonlocal counter
            counter += 1
            response = await http.post(
                "/mcp/apps",
                json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params},
                headers={
                    **(headers or {}),
                    **({"Authorization": f"Bearer {credential}"} if credential else {}),
                },
            )
            if method == "initialize" and response.status_code == 200:
                http.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
                http.headers["Mcp-Protocol-Version"] = "2025-11-25"
            assert response.status_code == 200, response.text
            bodies = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            return bodies[-1] if bodies else response.json()

        initialize = {
            "protocolVersion": "2025-11-25",
            "capabilities": {
                "extensions": {
                    "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}
                }
            },
            "clientInfo": {"name": "acceptance-host", "version": "1"},
        }
        await rpc("initialize", initialize)
        listing = (await rpc("tools/list", {}))["result"]["tools"]
        by_name = {tool["name"]: tool for tool in listing}
        assert set(by_name) == {descriptor["toolName"], descriptor2["toolName"], "panels.call"}
        assert by_name["panels.call"]["_meta"]["ui"]["visibility"] == ["app"]
        assert (
            by_name[descriptor["toolName"]]["_meta"]["ui"]["resourceUri"]
            == descriptor["resourceUri"]
        )
        resource = (await rpc("resources/read", {"uri": descriptor["resourceUri"]}))["result"][
            "contents"
        ][0]
        assert resource["mimeType"] == "text/html;profile=mcp-app"
        assert resource["text"].index("window.wot =") < resource["text"].index(markup)
        assert token not in resource["text"] and token2 not in resource["text"]
        opened = (await rpc("tools/call", {"name": descriptor["toolName"], "arguments": {}}))[
            "result"
        ]
        grant = opened["_meta"]["dev.wotbot/panel-launch"]["grant"]
        opened2 = (await rpc("tools/call", {"name": descriptor2["toolName"], "arguments": {}}))[
            "result"
        ]
        grant2 = opened2["_meta"]["dev.wotbot/panel-launch"]["grant"]

        async def panel_call(tool, arguments, grant_value=grant):
            return (
                await rpc(
                    "tools/call",
                    {
                        "name": "panels.call",
                        "arguments": {"grant": grant_value, "tool": tool, "arguments": arguments},
                    },
                )
            )["result"]

        read = await panel_call(
            "things.read_property", {"thingId": "lamp", "propertyName": "power"}
        )
        assert read["structuredContent"]["result"] == binary
        write = await panel_call(
            "things.write_property",
            {
                "thingId": "lamp",
                "propertyName": "power",
                "valueBase64": "AQI=",
                "valueContentType": "image/png",
            },
        )
        assert write["structuredContent"]["result"] is True
        assert device.write_property.call_args.kwargs["value_base64"] == "AQI="
        action = await panel_call(
            "things.invoke_action",
            {
                "thingId": "lamp",
                "actionName": "media",
                "inputBase64": "AQI=",
                "inputContentType": "image/png",
            },
        )
        assert action["structuredContent"]["result"] == binary
        assert device.invoke_action.call_args.kwargs["input_base64"] == "AQI="
        device.invoke_action.return_value = result({"status": "credential_rejected"})
        challenge = await panel_call(
            "things.invoke_action", {"thingId": "lamp", "actionName": "media"}
        )
        assert "credential" in challenge["structuredContent"]["result"]["error"]
        device.invoke_action.return_value = result(binary)
        denied = await panel_call(
            "things.write_property", {"thingId": "other", "propertyName": "power", "value": True}
        )
        assert denied["isError"] is True
        assert (
            await panel_call(
                "things.read_property", {"thingId": "lamp", "propertyName": "power"}, "forged"
            )
        )["isError"]
        # Subscribing hands back the stream position captured before it, and
        # the panel carries that forward itself -- the server stores no cursor.
        observed = await panel_call(
            "things.observe_property", {"thingId": "lamp", "propertyName": "power"}
        )
        cursor = observed["structuredContent"]["result"]["cursor"]
        settings = Settings()
        redis_client = redis.from_url(settings.redis_url)
        await redis_client.xadd(
            settings.wot_runtime_stream,
            {
                "event_type": "property_observed",
                "thing_id": "lamp",
                "name": "power",
                "subscription_id": "sub-first",
                "content_type": "image/png",
                "payload_base64": base64.b64encode(b"\x01\x02").decode(),
                "timestamp": "now",
            },
        )
        event = await panel_call(
            "things.next_subscription_event",
            {"subscriptionId": "sub-first", "timeoutMs": 100, "cursor": cursor},
        )
        assert event["structuredContent"]["result"]["event"]["value"] == binary
        assert event["structuredContent"]["result"]["nextCursor"] > cursor
        cross_panel = await panel_call(
            "things.next_subscription_event", {"subscriptionId": "sub-first"}, grant2
        )
        assert cross_panel["isError"] is True
        await panel_call("things.unsubscribe", {"subscriptionId": "sub-first"})
        assert (
            await panel_call("things.next_subscription_event", {"subscriptionId": "sub-first"})
        )["isError"]
        await redis_client.aclose()
        # Saved UI links follow edits. MCP source and its allowed operations stay at generation version.
        with get_session_factory()() as session:
            PanelService(session).update_panel(
                descriptor["panelId"],
                html="<p>Edited</p>",
                capabilities=[{"thingId": "new", "affordances": ["x"], "ops": ["invokeAction"]}],
            )
        same_resource = (await rpc("resources/read", {"uri": descriptor["resourceUri"]}))["result"][
            "contents"
        ][0]
        assert same_resource["text"] == resource["text"]
        assert (await panel_call("things.invoke_action", {"thingId": "new", "actionName": "x"}))[
            "isError"
        ]
        # A second key of the same administrator has a separate MCP session and no resources.
        first_session = http.headers.pop("Mcp-Session-Id")
        await rpc("initialize", initialize, credential=token2)
        assert (await rpc("resources/list", {}, credential=token2))["result"].get(
            "resources", []
        ) == []
        assert "error" in await rpc(
            "resources/read", {"uri": descriptor["resourceUri"]}, credential=token2
        )
        assert (
            await rpc(
                "tools/call",
                {
                    "name": "panels.call",
                    "arguments": {
                        "grant": grant,
                        "tool": "things.read_property",
                        "arguments": {"thingId": "lamp", "propertyName": "power"},
                    },
                },
                credential=token2,
            )
        )["result"]["isError"]
        http.headers["Mcp-Session-Id"] = first_session
        from wotbot.discovery.store import client_for
        from wotbot.mcp_apps.grants import PanelGrantStore

        # Launch grants are Redis capabilities, like artifact download links.
        await client_for(Settings().redis_url).delete(PanelGrantStore._key(grant2))
        assert (
            await panel_call(
                "things.read_property", {"thingId": "lamp", "propertyName": "power"}, grant2
            )
        )["isError"]
        # Re-wrapped from the pinned markup on every read, so a bridge fix
        # reaches panels generated before it. The old design stored the wrapped
        # bytes once and could never pick this up.
        with patch("wotbot.mcp_apps.render._bridge", lambda: "/* patched-bridge-marker */"):
            reread = await rpc("resources/read", {"uri": descriptor["resourceUri"]})
        assert "patched-bridge-marker" in reread["result"]["contents"][0]["text"]
        assert markup in reread["result"]["contents"][0]["text"]

        with get_session_factory()() as session:
            PanelService(session).delete_panel(descriptor["panelId"])
        assert await ArtifactStore().get(artifact.artifact_id, owner=owner) is None
        assert "error" in await rpc("resources/read", {"uri": descriptor["resourceUri"]})
        assert (
            await panel_call("things.read_property", {"thingId": "lamp", "propertyName": "power"})
        )["isError"]
        assert (
            await http.post("/mcp/apps", json={}, headers={"Authorization": f"Bearer {limited}"})
        ).status_code == 403
        assert (
            await http.post("/mcp/apps", json={}, headers={"Authorization": ""})
        ).status_code == 401


async def test_streaming_http_history_limits_terminal_subscription_and_cursor_pages(environment):
    from a2a.types import ListTasksRequest
    from a2a.utils.errors import UnsupportedOperationError

    owner, token, other, _, _ = environment

    async def node(state):
        return {"messages": [AIMessage(content="Streamed answer")]}

    async with running(graph_for(node)) as runtime:
        app = FastAPI()
        app.state.a2a_runtime = runtime
        install_a2a(app, Settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        ) as http:
            client = ClientFactory(
                ClientConfig(
                    streaming=True, httpx_client=http, supported_protocol_bindings=["HTTP+JSON"]
                )
            ).create(build_agent_card(Settings()))
            original = request()
            original.configuration.history_length = 0
            events = [event async for event in client.send_message(original)]
            assert events[0].HasField("task") and not events[0].task.history
            assert all(
                e.HasField("status_update") or e.HasField("artifact_update") for e in events[1:]
            )
            assert events[-1].status_update.status.state == TaskState.TASK_STATE_COMPLETED
            assert events[-1].status_update.status.message.parts[0].text == "Streamed answer"
            task_id = events[0].task.id
            no_history = await http.get(f"/a2a/v1/tasks/{task_id}?historyLength=0")
            assert "history" not in no_history.json()
            # A duplicate stream request returns the original task snapshot without execution.
            replay = [e async for e in client.send_message(original)]
            assert len(replay) == 1 and replay[0].task.id == task_id
            with pytest.raises(UnsupportedOperationError):
                await anext(runtime.subscribe(owner, task_id, terminal_error=True))
            for _ in range(2):
                await http.post("/a2a/v1/message:send", json=MessageToDict(request()))
            page, total, cursor = runtime.store.list(owner, ListTasksRequest(page_size=1))
            assert total == 3 and len(page) == 1 and cursor
            next_page, _, next_cursor = runtime.store.list(
                owner, ListTasksRequest(page_size=1, page_token=cursor)
            )
            assert len(next_page) == 1 and next_page[0].id != page[0].id and next_cursor
            with pytest.raises(InvalidParamsError):
                runtime.store.list(other, ListTasksRequest(page_size=1, page_token=cursor))
            filtered, _, _ = runtime.store.list(
                owner, ListTasksRequest(status_timestamp_after=page[0].status.timestamp)
            )
            assert filtered[0].id == page[0].id


async def test_second_execution_process_cannot_recover_active_tasks(environment):
    async def node(state):
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)):
        second = A2ARuntime(graph=graph_for(node), registry=RunRegistry(), settings=Settings())
        with pytest.raises(RuntimeError, match="one API execution process"):
            await second.start()


async def test_http_return_immediately_and_cancel_paused_context(environment):
    owner, token, *_ = environment
    entered, release = asyncio.Event(), asyncio.Event()

    async def node(state):
        if state["messages"][-1].content == "new":
            return {"messages": [AIMessage(content="New task")]}
        entered.set()
        await release.wait()
        interrupt({"kind": "confirmation", "question": "Continue?"})
        return {"messages": [AIMessage(content="Confirmed")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        app = FastAPI()
        app.state.a2a_runtime = runtime
        install_a2a(app, Settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        ) as http:
            original = request()
            original.configuration.return_immediately = True
            response = await asyncio.wait_for(
                http.post("/a2a/v1/message:send", json=MessageToDict(original)), 3
            )
            task = response.json()["task"]
            assert task["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
            await entered.wait()
            release.set()
            runner = runtime.runners[task["id"]]
            await runner
            assert (
                runtime.store.get(owner, task["id"]).status.state
                == TaskState.TASK_STATE_INPUT_REQUIRED
            )
            cancelled = await http.post(f"/a2a/v1/tasks/{task['id']}:cancel", json={})
            assert (
                cancelled.status_code == 200
                and cancelled.json()["status"]["state"] == "TASK_STATE_CANCELED"
            )
            followup = await runtime.admit(owner, request("new", context=task["contextId"]))
            assert (
                await finish(runtime, followup.task)
            ).status.state == TaskState.TASK_STATE_COMPLETED


async def test_export_failures_survive_a_pause_and_restart(environment):
    from wotbot.a2a.outputs import ArtifactCollector

    owner = environment[0]

    async def export(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "export", "name": "run_code", "args": {"code": "export()"}}],
                ),
                ToolMessage(
                    id="export-result",
                    tool_call_id="export",
                    content='{"artifacts":[{"kind":"file","filename":"missing.csv"}]}',
                ),
            ]
        }

    async def confirm(state):
        interrupt({"kind": "confirmation"})
        return {"messages": [AIMessage(content="Finished remaining work")]}

    async def fetch(settings, item):
        raise FileNotFoundError()

    builder = StateGraph(State)
    builder.add_node("export", export)
    builder.add_node("confirm", confirm)
    builder.add_edge(START, "export")
    builder.add_edge("export", "confirm")
    graph = builder.compile(checkpointer=InMemorySaver())

    def factory(**kw):
        return ArtifactCollector(settings=Settings(), fetch=fetch, **kw)

    async with running(graph, collector_factory=factory) as runtime:
        runtime.owner = owner
        admitted = await runtime.admit(owner, request())
        paused = await finish(runtime, admitted.task)
        assert paused.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert "export failures" in paused.status.message.parts[0].text
    async with running(graph, collector_factory=factory) as runtime:
        runtime.owner = owner
        pending = MessageToDict(paused.status.message.parts[-1])["data"]["requests"][0]
        resumed = await runtime.admit(
            owner,
            request(
                task=paused.id,
                data={
                    "requestId": pending["requestId"],
                    "response": {"approved": True},
                },
            ),
        )
        finished = await finish(runtime, resumed.task)
        assert finished.status.state == TaskState.TASK_STATE_FAILED
        assert "could not be exported" in finished.status.message.parts[0].text
        assert finished.history[-1].parts[0].text == "Finished remaining work"


@pytest.mark.parametrize("endpoint", ["message:send", "message:stream"])
async def test_disconnect_during_admission_does_not_strand_task(environment, monkeypatch, endpoint):
    import threading
    from contextlib import suppress

    from a2a.types import ListTasksRequest

    owner, token, *_ = environment
    entered, release = threading.Event(), threading.Event()
    executed = asyncio.Event()

    async def node(state):
        executed.set()
        return {"messages": [AIMessage(content="Completed after disconnect")]}

    async with running(graph_for(node)) as runtime:
        original_admit = runtime.store.admit

        def slow_admit(*args):
            entered.set()
            assert release.wait(5)
            return original_admit(*args)

        monkeypatch.setattr(runtime.store, "admit", slow_admit)
        app = FastAPI()
        app.state.a2a_runtime = runtime
        install_a2a(app, Settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        ) as http:
            connection = asyncio.create_task(
                http.post(
                    f"/a2a/v1/{endpoint}",
                    json=MessageToDict(request(id="disconnected-admission")),
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                connection.cancel()
                with suppress(asyncio.CancelledError):
                    await connection
            finally:
                release.set()
            await asyncio.wait_for(executed.wait(), 5)
            await asyncio.gather(*list(runtime.runners.values()))
            tasks, total, _ = runtime.store.list(owner, ListTasksRequest())
            assert total == 1 and tasks[0].status.state == TaskState.TASK_STATE_COMPLETED


async def test_temporary_download_links_cover_streams_images_files_and_old_tasks(environment):
    executor_files = {
        "chart.png": ("chart.png", b"\x89PNG", "image/png"),
        "data.csv": ("data.csv", b"value\n42", "text/csv"),
    }
    async with fake_executor(executor_files) as executor_url:
        await _exercise_download_links(
            environment,
            Settings(a2a_download_url_ttl_seconds=3600, code_executor_url=executor_url),
        )


async def _exercise_download_links(environment, settings):
    from urllib.parse import parse_qs, urlsplit

    from wotbot.a2a.downloads import ArtifactDownloadLinks
    from wotbot.discovery.store import client_for

    owner, token, other, token2, _ = environment

    async def node(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "generate", "name": "run_code", "args": {"code": "generate()"}}
                    ],
                ),
                ToolMessage(
                    id="generated",
                    tool_call_id="generate",
                    content=json.dumps(
                        {
                            "ok": True,
                            "artifacts": [
                                {"kind": "image", "filename": "chart.png"},
                                {"kind": "file", "filename": "data.csv"},
                            ],
                        }
                    ),
                ),
                AIMessage(content="Generated image and file"),
            ]
        }

    async with running(graph_for(node), settings=settings) as runtime:
        app = FastAPI()
        app.state.a2a_runtime = runtime
        install_a2a(app, settings)
        logged_queries = []

        async def observed_app(scope, receive, send):
            await app(scope, receive, send)
            logged_queries.append(scope.get("query_string", b""))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=observed_app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        ) as http:
            client = ClientFactory(
                ClientConfig(
                    streaming=True,
                    httpx_client=http,
                    supported_protocol_bindings=["HTTP+JSON"],
                )
            ).create(build_agent_card(settings))
            original = request(id="download-fixture")
            events = [event async for event in client.send_message(original)]
            updates = [
                event.artifact_update.artifact
                for event in events
                if event.HasField("artifact_update")
                and any(part.HasField("url") for part in event.artifact_update.artifact.parts)
            ]
            assert len(updates) == 2
            task_id = events[0].task.id
            for artifact, expected, mime in zip(
                updates, [b"\x89PNG", b"value\n42"], ["image/png", "text/csv"], strict=True
            ):
                url = artifact.parts[0].url
                metadata = MessageToDict(artifact)["metadata"]
                assert "downloadToken=" in url and token not in url
                assert metadata["downloadUrl"] == url
                assert metadata["downloadUrlExpiresAt"] < metadata["expiresAt"]
                response = await http.get(url, headers={"Authorization": ""})
                assert response.status_code == 200 and response.content == expected
                assert response.headers["content-type"].startswith(mime)
                assert response.headers["referrer-policy"] == "no-referrer"
                assert logged_queries[-1] == b"", "Access logs must not receive the download token"
                canonical = metadata["authenticatedDownloadUrl"]
                assert (await http.get(canonical, headers={"Authorization": ""})).status_code == 401
                assert (await http.get(canonical)).status_code == 200
                assert (
                    await http.get(url, headers={"Authorization": f"Bearer {token2}"})
                ).status_code == 404
                assert (await http.head(url, headers={"Authorization": ""})).status_code == 200

            image, file = updates
            image_url = image.parts[0].url
            download_token = parse_qs(urlsplit(image_url).query)["downloadToken"][0]
            # The capability is limited to a single artifact and cannot execute
            # tasks or open resources, even when sent to another A2A endpoint.
            swapped = image_url.replace(image.artifact_id, file.artifact_id)
            assert (await http.get(swapped, headers={"Authorization": ""})).status_code == 410
            assert (
                await http.get(image_url + "x", headers={"Authorization": ""})
            ).status_code == 410
            assert (
                await http.get(
                    image_url + "&downloadToken=" + download_token,
                    headers={"Authorization": ""},
                )
            ).status_code == 410
            assert (
                await http.post(
                    "/a2a/v1/message:send?downloadToken=" + download_token,
                    headers={"Authorization": ""},
                    json=MessageToDict(request()),
                )
            ).status_code == 401
            redis = client_for(settings.redis_url)
            grant_key = ArtifactDownloadLinks._key(download_token)
            assert 0 < await redis.pttl(grant_key) <= 3600 * 1000
            await redis.delete(grant_key)  # Redis expiry has the same missing-key result.
            expired = await http.get(image_url, headers={"Authorization": ""})
            assert expired.status_code == 410 and "expired" in expired.json()["error"]
            # Existing persisted manifests keep their authenticated URL; fresh
            # links are added at response time to GET, LIST and duplicate sends.
            stored = runtime.store.get(owner, task_id)
            assert all("downloadToken=" not in a.parts[0].url for a in stored.artifacts)
            assert "downloadUrlExpiresAt" not in MessageToDict(stored.artifacts[0])["metadata"]
            fresh = (await http.get("/a2a/v1/tasks/" + task_id)).json()
            fresh_url = fresh["artifacts"][0]["parts"][0]["url"]
            assert fresh_url != image_url
            assert (await http.get(fresh_url, headers={"Authorization": ""})).status_code == 200
            listed = (await http.get("/a2a/v1/tasks?includeArtifacts=true")).json()
            assert "downloadToken=" in listed["tasks"][0]["artifacts"][0]["parts"][0]["url"]
            duplicate = (
                await http.post("/a2a/v1/message:send", json=MessageToDict(original))
            ).json()["task"]
            assert duplicate["id"] == task_id
            assert "downloadToken=" in duplicate["artifacts"][0]["parts"][0]["url"]
            with get_session_factory()() as session:
                session.get(A2AArtifactRecord, file.artifact_id).expires_at = utc_now() + timedelta(
                    seconds=15
                )
                session.commit()
            short = (await http.get("/a2a/v1/tasks/" + task_id)).json()["artifacts"][1]
            assert short["metadata"]["downloadUrlExpiresAt"] <= short["metadata"]["expiresAt"]
            short_token = parse_qs(urlsplit(short["parts"][0]["url"]).query)["downloadToken"][0]
            assert 0 < await redis.pttl(ArtifactDownloadLinks._key(short_token)) <= 15_000
            with get_session_factory()() as session:
                session.get(A2AArtifactRecord, file.artifact_id).expires_at = utc_now() - timedelta(
                    seconds=1
                )
                session.commit()
            assert (
                await http.get(short["parts"][0]["url"], headers={"Authorization": ""})
            ).status_code == 410
            with get_session_factory()() as session:
                session.delete(session.get(A2AArtifactRecord, image.artifact_id))
                session.commit()
            assert (await http.get(fresh_url, headers={"Authorization": ""})).status_code == 404


@pytest.mark.parametrize("change", ["revoked", "expired", "scope_removed"])
async def test_download_grants_stop_working_when_the_owner_key_loses_access(environment, change):
    from wotbot.a2a.artifacts import ArtifactStore
    from wotbot.a2a.downloads import ArtifactDownloadLinks

    owner, token, *_ = environment
    store = ArtifactStore()
    artifact_id = str(uuid4())
    await store.put(
        artifact_id=artifact_id,
        task_id="download-test",
        owner=owner,
        name="image.png",
        media_type="image/png",
        executor_artifact_id="image.png",
        metadata={"expiresAt": (utc_now() + timedelta(days=1)).isoformat()},
    )
    record = await store.get(artifact_id, owner=owner)
    async with fake_executor({"image.png": ("image.png", b"PNG", "image/png")}) as executor_url:
        settings = Settings(code_executor_url=executor_url)
        link_token, _ = await ArtifactDownloadLinks(settings)._issue(record)
        app = FastAPI()
        install_a2a(app, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as http:
            url = f"/a2a/artifacts/{artifact_id}?downloadToken={link_token}"
            assert (await http.get(url)).status_code == 200
            with get_session_factory()() as session:
                key = session.get(ApiKey, owner)
                if change == "revoked":
                    key.is_active = False
                elif change == "expired":
                    key.expires_at = utc_now() - timedelta(seconds=1)
                else:
                    key.scopes = []
                session.commit()
            assert (await http.get(url)).status_code == 401


@pytest.mark.parametrize("reuse_as", ["continuation", "new_task"])
async def test_message_identity_is_unique_across_an_owners_tasks(environment, reuse_as):
    owner, _, other, *_ = environment
    effects = []

    async def node(state):
        answer = interrupt({"kind": "confirmation", "question": "Proceed?"})
        if answer["approved"]:
            effects.append("action")
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner

        async def pause(key):
            admitted = await runtime.admit(key, request())
            runner = runtime.runners[admitted.task.id]
            await asyncio.wait_for(asyncio.shield(runner), 3)
            task = runtime.store.get(key, admitted.task.id)
            pending = MessageToDict(task.status.message.parts[-1])["data"]["requests"][0]
            return request(
                id="shared-reply",
                task=task.id,
                context=task.context_id,
                data={"requestId": pending["requestId"], "response": {"approved": True}},
            )

        reply = await pause(owner)
        resumed = await runtime.admit(owner, reply)
        await finish(runtime, resumed.task)
        assert not (await runtime.admit(owner, reply)).execute
        reused = await pause(owner) if reuse_as == "continuation" else request(id="shared-reply")
        with pytest.raises(InvalidParamsError, match="messageId"):
            await runtime.admit(owner, reused)
        assert effects == ["action"]
        # Message IDs are scoped to a key, not its administrator or the server.
        other_reply = await pause(other)
        resumed_other = await runtime.admit(other, other_reply)
        await asyncio.wait_for(asyncio.shield(runtime.runners[resumed_other.task.id]), 3)
        assert effects == ["action", "action"]


async def test_retirement_serializes_with_followup_admission(environment, monkeypatch):
    owner = environment[0]

    async def node(state):
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        first = await runtime.admit(owner, request())
        await finish(runtime, first.task)
        with get_session_factory()() as session:
            session.get(A2ATaskRecord, first.task.id).expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
        entered, release, admission_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_delete = runtime.graph.checkpointer.adelete_thread

        async def delayed_delete(thread_id):
            entered.set()
            await release.wait()
            await original_delete(thread_id)

        async def followup():
            admission_started.set()
            return await runtime.admit(owner, request(context=first.task.context_id))

        monkeypatch.setattr(runtime.graph.checkpointer, "adelete_thread", delayed_delete)
        sweeping = asyncio.create_task(runtime.sweep())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            pending = asyncio.create_task(followup())
            await asyncio.wait_for(admission_started.wait(), 3)
            assert runtime.admission_lock.locked()
            assert not pending.done()
        finally:
            release.set()
            await sweeping
        with pytest.raises(InvalidParamsError, match="Unknown contextId"):
            await pending


async def test_context_deletion_rechecks_for_new_tasks(environment):
    owner = environment[0]

    async def node(state):
        return {"messages": [AIMessage(content="Done")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        first = await runtime.admit(owner, request())
        await finish(runtime, first.task)
        with get_session_factory()() as session:
            session.get(A2ATaskRecord, first.task.id).expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
        retired = await asyncio.to_thread(runtime.store.prune)
        followup = await runtime.admit(owner, request(context=first.task.context_id))
        await finish(runtime, followup.task)
        assert await asyncio.to_thread(runtime.store.delete_contexts, retired) == 0
        assert runtime.store.get(owner, followup.task.id).id == followup.task.id
