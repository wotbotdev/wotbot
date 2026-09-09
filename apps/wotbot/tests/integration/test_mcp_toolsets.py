"""MCP profiles exercise the real shared database, graph lifecycle and SDK."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from mcp.types import CallToolRequestParams

from .test_a2a import graph_for, request, running, finish, fake_executor
from .test_a2a import environment as environment, anyio_backend as anyio_backend
from wotbot.agent_api.catalog import PUBLIC_NAMES, argument_schema, validate_arguments
from wotbot.agent_api.raw import build_raw_graph
from wotbot.agent_api.subscriptions import RawSubscriptions
from wotbot.agent.tools.contracts import tool
from wotbot.auth.models import User
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.core.time import utc_now
from wotbot.mcp.server import MCPToolRuntime, profile_tools
from wotbot.a2a.models import AgentSubscriptionRecord
from wotbot.threads.models import Thread

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def principal(owner):
    return User(
        user_id="same-admin", api_key_id=owner, scopes=["agent:invoke"], auth_type="api_key"
    )


def install_profiles(runtime, monkeypatch, owner):
    monkeypatch.setattr("wotbot.mcp.server._request_user", lambda: principal(owner))
    return {
        profile: MCPToolRuntime(
            profile=profile, get_runtime=lambda: runtime.service, settings=runtime.settings
        )
        for profile in ("assistant", "intents", "raw")
    }


async def call(profile, name, **arguments):
    return await profile._call_tool(None, CallToolRequestParams(name=name, arguments=arguments))


async def test_shared_contexts_raw_isolation_and_catalog(environment, monkeypatch):
    owner, _, other, *_ = environment
    seen = []

    async def node(state, config: RunnableConfig):
        seen.append(config["configurable"].get("forced_intent"))
        return {"messages": [AIMessage(content="Assistant answer")]}

    async with running(graph_for(node)) as runtime:
        runtime.owner = owner
        runtime.service.raw_graph = build_raw_graph(InMemorySaver())
        profiles = install_profiles(runtime, monkeypatch, owner)
        first = await runtime.admit(owner, request())
        await finish(runtime, first.task)
        for intent in ("chat", "control", "analysis", "jobs", "virtual_things", "discovery"):
            result = await call(
                profiles["intents"],
                "intent." + intent,
                requestId=str(uuid4()),
                message="Continue",
                contextId=first.task.context_id,
            )
            assert not result.is_error, result
            assert result.structured_content["contextId"] == first.task.context_id
            assert seen[-1] == intent
        normal = await call(
            profiles["assistant"],
            "ask_wotbot",
            requestId="auto",
            message="Again",
            contextId=first.task.context_id,
        )
        assert not normal.is_error and seen[-1] is None
        raw = await call(profiles["raw"], "get_current_time", requestId="raw-clock", arguments={})
        assert not raw.is_error, raw
        raw_task = raw.structured_content
        assert raw_task["status"] == "completed"
        assert len(seen) == 8  # Raw execution never entered the assistant graph.
        with get_session_factory()() as session:
            thread = session.get(Thread, raw_task["contextId"])
            assert (
                thread.kind == "mcp_raw" and not thread.visible and thread.owner_api_key_id == owner
            )
        for profile, name, arguments in [
            ("assistant", "get_current_time", {"arguments": {}}),
            ("raw", "ask_wotbot", {"message": "Hello"}),
            ("raw", "get_current_time", {"arguments": {}, "contextId": first.task.context_id}),
            ("assistant", "ask_wotbot", {"message": "Hello", "contextId": raw_task["contextId"]}),
        ]:
            assert (
                await call(profiles[profile], name, requestId=str(uuid4()), **arguments)
            ).is_error
        assert (await call(profiles["assistant"], "task.get", taskId=raw_task["taskId"])).is_error
        assert (await call(profiles["raw"], "task.get", taskId=first.task.id)).is_error
        monkeypatch.setattr("wotbot.mcp.server._request_user", lambda: principal(other))
        assert (await call(profiles["raw"], "task.get", taskId=raw_task["taskId"])).is_error
        assert (
            await call(
                profiles["raw"],
                "get_current_time",
                requestId=str(uuid4()),
                arguments={},
                contextId=raw_task["contextId"],
            )
        ).is_error


async def test_raw_dedup_disconnect_cancel_and_resume(environment, monkeypatch):
    owner = environment[0]
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def write(value: int):
        """Perform a test device write."""
        entered.set()
        await release.wait()
        effects.append(value)
        return {"value": value}

    async def forbidden(state):
        raise AssertionError("Raw calls must not invoke the assistant")

    async with running(graph_for(forbidden)) as runtime:
        # Override only the public implementation; the public name and schemas remain real.
        write.name = "wot_write_property"

        @tool
        async def compatible(thing_id: str, property_name: str, value: int, config: RunnableConfig):
            """A fake device with the real public argument names."""
            assert config["configurable"]["thread_id"] != "default"
            return await write.ainvoke({"value": value})

        runtime.service.raw_graph = build_raw_graph(
            InMemorySaver(), tools={"wot_write_property": compatible}
        )
        raw = install_profiles(runtime, monkeypatch, owner)["raw"]
        arguments = {
            "requestId": "write-once",
            "arguments": {"thing_id": "lamp", "property_name": "level", "value": 42},
            "waitSeconds": 0,
        }
        first = (await call(raw, "wot_write_property", **arguments)).structured_content
        await entered.wait()
        retry = (await call(raw, "wot_write_property", **arguments)).structured_content
        assert first["taskId"] == retry["taskId"]
        changed = {**arguments, "arguments": {**arguments["arguments"], "value": 50}}
        assert (await call(raw, "wot_write_property", **changed)).is_error
        waiting = asyncio.create_task(call(raw, "task.get", taskId=first["taskId"], waitSeconds=30))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not runtime.runners[first["taskId"]].done()
        release.set()
        done = await call(raw, "task.get", taskId=first["taskId"], waitSeconds=5)
        assert done.structured_content["status"] == "completed"
        assert effects == [42]
        assert done.structured_content["result"] == {"value": 42}
        artifact = done.structured_content["artifacts"][0]
        assert (
            await call(raw, "artifact.get", artifactId=artifact["artifactId"])
        ).structured_content["artifact"]["parts"][0]["data"]["result"] == {"value": 42}

    @tool
    async def confirmation(thing_id: str, property_name: str, value: int):
        """Pause before changing a device."""
        reply = interrupt({"kind": "confirmation", "question": "Proceed?"})
        if reply["approved"]:
            effects.append(value)
        return {"approved": reply["approved"]}

    async with running(graph_for(forbidden)) as runtime:
        runtime.service.raw_graph = build_raw_graph(
            InMemorySaver(), tools={"wot_write_property": confirmation}
        )
        raw = install_profiles(runtime, monkeypatch, owner)["raw"]
        paused = (
            await call(
                raw, "wot_write_property", requestId="pause", arguments=arguments["arguments"]
            )
        ).structured_content
        assert paused["status"] == "input_required", paused
        assert effects == [42]
        assert (
            await call(
                raw,
                "task.resume",
                taskId=paused["taskId"],
                requestId="bad-reply",
                replies=[
                    {"requestId": paused["pending"][0]["requestId"], "response": {"goto": "finish"}}
                ],
            )
        ).is_error
        resumed = await call(
            raw,
            "task.resume",
            taskId=paused["taskId"],
            requestId="approve",
            replies=[
                {"requestId": paused["pending"][0]["requestId"], "response": {"approved": True}}
            ],
        )
        assert resumed.structured_content["status"] == "completed", resumed
        assert effects == [42, 42]


async def test_raw_subscription_ownership_binary_expiry_and_revocation(environment, monkeypatch):
    owner, _, other, *_ = environment
    client = AsyncMock()
    client.observe_property.return_value = {
        "subscription": {"subscriptionId": "runtime-one", "streamName": "private"}
    }
    client.subscription_status.return_value = {"exists": True}
    client.remove_subscription.return_value = {"ok": True}
    service = RawSubscriptions(Settings(), client=client)
    context_id = str(uuid4())
    with get_session_factory()() as session:
        session.add(
            Thread(
                id=context_id,
                kind="mcp_raw",
                visible=False,
                title="raw",
                owner_api_key_id=owner,
                created_at=utc_now().isoformat(),
                updated_at=utc_now().isoformat(),
            )
        )
        session.commit()
    config = {"configurable": {"owner": owner, "thread_id": context_id}}
    output = await service.execute(
        "wot_observe_property", {"thing_id": "camera", "property_name": "image"}, config
    )
    identifier = output["subscription"]["subscriptionId"]
    assert identifier != "runtime-one"
    assert (
        client.observe_property.call_args.kwargs["subscription_namespace"]
        == "mcp-raw:" + context_id
    )
    with pytest.raises(ValueError):
        await service.poll(other, context_id, identifier)
    with pytest.raises(ValueError):
        await service.poll(owner, "other-context", identifier)
    binary = {"kind": "binary", "bodyBase64": "AQI=", "contentType": "image/png"}
    monkeypatch.setattr(
        "wotbot.agent_api.subscriptions.next_subscription_event",
        AsyncMock(
            return_value=({"event": {"subscriptionId": "runtime-one", "value": binary}}, "1-0")
        ),
    )
    event = await service.poll(owner, context_id, identifier, cursor="0-0")
    assert event["event"]["subscriptionId"] == identifier and event["event"]["value"] == binary
    with get_session_factory()() as session:
        session.get(AgentSubscriptionRecord, identifier).expires_at = utc_now() - timedelta(
            seconds=1
        )
        session.commit()
    await service.sweep()
    client.remove_subscription.assert_awaited_once_with(subscription_id="runtime-one")
    with pytest.raises(ValueError):
        await service.poll(owner, context_id, identifier)


async def test_public_schemas_reject_injected_fields(environment):
    assert set(profile_tools("raw")) == PUBLIC_NAMES | {
        "task.get",
        "task.list",
        "task.resume",
        "task.cancel",
        "artifact.get",
        "artifact.list",
        "subscription.poll",
    }
    assert not ({"route_to", "ask_job_user", "submit_job_record"} & PUBLIC_NAMES)
    for name in PUBLIC_NAMES:
        assert not (
            {"created_from_thread_id", "config", "thread_id", "owner"}
            & argument_schema(name)["properties"].keys()
        )
    for field in ("config", "thread_id", "owner", "created_from_thread_id"):
        with pytest.raises(ValueError):
            validate_arguments("run_code", {"code": "pass", field: "forged"})


@pytest.fixture(autouse=True)
async def close_stream_clients():
    yield
    from wotbot.mcp_apps.service import close_runtime_stream_clients

    await close_runtime_stream_clients()


@asynccontextmanager
async def serve(app):
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="error")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await serving


@pytest.mark.parametrize("profile", ["assistant", "intents", "raw"])
async def test_official_mcp_client_profiles_panels_and_auth(environment, profile):
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from wotbot.a2a.server import install_downloads
    from wotbot.agent_api.outputs import ArtifactCollector

    owner, token, other, other_token, limited = environment

    async def node(state):
        return {"messages": [AIMessage(content="SDK assistant answer")]}

    async with running(graph_for(node)) as runtime:
        runtime.service.raw_graph = build_raw_graph(InMemorySaver())
        runtime.owner = owner
        # Save a real panel through the same output processor used by raw/assistant runs.
        admitted = await runtime.admit(owner, request())
        await finish(runtime, admitted.task)
        collector = ArtifactCollector(
            settings=Settings(),
            owner=owner,
            task_id=admitted.task.id,
            thread_id=admitted.task.context_id,
        )
        output = await collector.record_tool_result(
            "create_web_interface",
            {"title": "SDK panel", "html": "<p>SDK panel</p>"},
            {"artifacts": [{"kind": "web", "capabilities": []}]},
            "panel-call",
        )
        panel = output[0].parts[0].data
        mcp = MCPToolRuntime(
            profile=profile, get_runtime=lambda: runtime.service, settings=Settings()
        )
        app = FastAPI()
        mcp.install(app)
        install_downloads(app, Settings())
        async with mcp.lifespan(), serve(app) as base:
            # Supply an allowed public host while exercising real network transport.
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + token, "Host": "localhost:8000"}
            ) as http:
                async with Client(
                    streamable_http_client(base + "/mcp/" + profile, http_client=http)
                ) as client:
                    listing = await client.list_tools()
                    names = {t.name for t in listing.tools}
                    assert set(profile_tools(profile)) <= names
                    assert panel["toolName"] in names
                    if profile == "raw":
                        result = await client.call_tool(
                            "get_current_time", {"requestId": "sdk-clock", "arguments": {}}
                        )
                    elif profile == "intents":
                        result = await client.call_tool(
                            "intent.chat", {"requestId": "sdk-chat", "message": "Hello"}
                        )
                    else:
                        result = await client.call_tool(
                            "ask_wotbot", {"requestId": "sdk-chat", "message": "Hello"}
                        )
                    assert not result.is_error, result
                    task = result.structured_content
                    assert task["status"] == "completed"
                    opened = await client.call_tool(panel["toolName"], {})
                    assert not opened.is_error, opened
                    assert opened.structured_content["mcpServerUrl"].endswith("/mcp/" + profile)
                    document = await client.read_resource(panel["resourceUri"])
                    assert "SDK panel" in document.contents[0].text
                    assert document.contents[0].text.index("window.wot =") < document.contents[
                        0
                    ].text.index("<p>SDK panel</p>")
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + other_token, "Host": "localhost:8000"}
            ) as http:
                async with Client(
                    streamable_http_client(base + "/mcp/" + profile, http_client=http)
                ) as client:
                    assert panel["toolName"] not in {
                        t.name for t in (await client.list_tools()).tools
                    }
                    assert (await client.call_tool("task.get", {"taskId": task["taskId"]})).is_error
                    from mcp.shared.exceptions import MCPError

                    with pytest.raises(MCPError):
                        await client.call_tool(panel["toolName"], {})
            async with httpx.AsyncClient(base_url=base, headers={"Host": "localhost:8000"}) as http:
                assert (
                    await http.post(
                        "/mcp/" + profile, json={}, headers={"Authorization": "Bearer " + limited}
                    )
                ).status_code == 403
                assert (await http.post("/mcp/" + profile, json={})).status_code == 401
                # A session ID is never sufficient authorization, even for another
                # valid key belonging to the same administrator.
                headers = {
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                }
                initialized = await http.post(
                    "/mcp/" + profile,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "session-isolation", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200
                headers.update(
                    {
                        "Mcp-Session-Id": initialized.headers["mcp-session-id"],
                        "Mcp-Protocol-Version": "2025-11-25",
                    }
                )
                await http.post(
                    "/mcp/" + profile,
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                stolen = await http.post(
                    "/mcp/" + profile,
                    headers={**headers, "Authorization": "Bearer " + other_token},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                assert stolen.status_code == 404
                owned = await http.post(
                    "/mcp/" + profile,
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                )
                assert owned.status_code == 200
                await http.delete("/mcp/" + profile, headers=headers)


async def test_mcp_image_file_links_and_expiry(environment, monkeypatch):
    from wotbot.agent_api.outputs import ArtifactCollector
    from wotbot.a2a.server import install_downloads
    from wotbot.a2a.models import A2AArtifactRecord

    owner, _, other, *_ = environment

    async def node(state):
        return {"messages": [AIMessage(content="done")]}

    async with fake_executor(
        {
            "chart": ("chart.png", b"png-bytes", "image/png"),
            "file": ("data.csv", b"value\n42", "text/csv"),
        }
    ) as executor:
        settings = Settings(code_executor_url=executor)
        async with running(graph_for(node), settings=settings) as runtime:
            runtime.owner = owner
            admitted = await runtime.admit(owner, request())
            await finish(runtime, admitted.task)
            collector = ArtifactCollector(
                settings=settings,
                owner=owner,
                task_id=admitted.task.id,
                thread_id=admitted.task.context_id,
            )
            outputs = await collector.record_tool_result(
                "run_code",
                {"code": "pass"},
                {"artifacts": [{"id": "chart", "kind": "image"}, {"id": "file", "kind": "file"}]},
                "export",
            )
            raw = install_profiles(runtime, monkeypatch, owner)["raw"]
            image_result = await call(raw, "artifact.get", artifactId=outputs[0].artifact_id)
            assert any(part.type == "image" for part in image_result.content), image_result
            first = image_result.structured_content["downloadUrl"]
            second = (
                await call(raw, "artifact.get", artifactId=outputs[0].artifact_id)
            ).structured_content["downloadUrl"]
            assert first != second and "/agent/artifacts/" in first
            app = FastAPI()
            install_downloads(app, settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                assert (await http.get(first)).content == b"png-bytes"
                assert (await http.head(first)).status_code == 200
                assert (
                    await http.get(first.replace("/agent/artifacts/", "/a2a/artifacts/"))
                ).status_code == 200
                with get_session_factory()() as session:
                    session.get(A2AArtifactRecord, outputs[0].artifact_id).expires_at = (
                        utc_now() - timedelta(seconds=1)
                    )
                    session.commit()
                assert (await http.get(first)).status_code == 410
            assert (await call(raw, "artifact.get", artifactId=outputs[0].artifact_id)).is_error
            monkeypatch.setattr("wotbot.mcp.server._request_user", lambda: principal(other))
            assert (await call(raw, "artifact.get", artifactId=outputs[1].artifact_id)).is_error


async def test_raw_credential_pause_survives_restart_and_cancel(environment, monkeypatch):
    owner = environment[0]
    credential_saved = False
    actions = []

    @tool
    async def device(thing_id: str, property_name: str, value: int):
        """A device which challenges before accepting an operation."""
        if not credential_saved:
            return {"status": "credential_required", "message": "Configure credentials"}
        actions.append(value)
        return {"ok": True}

    async def forbidden(state):
        raise AssertionError("Assistant graph was invoked")

    graph = graph_for(forbidden)
    raw_graph = build_raw_graph(InMemorySaver(), tools={"wot_write_property": device})
    async with running(graph, raw_graph=raw_graph) as runtime:
        raw = install_profiles(runtime, monkeypatch, owner)["raw"]
        paused = (
            await call(
                raw,
                "wot_write_property",
                requestId="credential-pause",
                arguments={"thing_id": "device", "property_name": "level", "value": 10},
            )
        ).structured_content
        assert paused["status"] == "auth_required" and not actions
    credential_saved = True
    async with running(graph, raw_graph=raw_graph) as runtime:
        raw = install_profiles(runtime, monkeypatch, owner)["raw"]
        snapshot = (await call(raw, "task.get", taskId=paused["taskId"])).structured_content
        assert snapshot["status"] == "auth_required"
        response = await call(
            raw,
            "task.resume",
            taskId=paused["taskId"],
            requestId="credentials-added",
            replies=[
                {
                    "requestId": snapshot["pending"][0]["requestId"],
                    "response": {"status": "credential_saved"},
                }
            ],
        )
        assert response.structured_content["status"] == "completed", response
        assert actions == [10]
        # A suspended task can also be cancelled without retrying the device.
        credential_saved = False
        another = (
            await call(
                raw,
                "wot_write_property",
                requestId="cancel-pause",
                contextId=paused["contextId"],
                arguments={"thing_id": "device", "property_name": "level", "value": 20},
            )
        ).structured_content
        cancelled = await call(raw, "task.cancel", taskId=another["taskId"])
        assert cancelled.structured_content["status"] == "canceled"
        assert actions == [10]
        credential_saved = True
        followup = await call(
            raw,
            "wot_write_property",
            requestId="after-cancel",
            contextId=paused["contextId"],
            arguments={"thing_id": "device", "property_name": "level", "value": 30},
        )
        assert followup.structured_content["status"] == "completed", followup
        assert actions == [10, 30]
        credential_saved = False
        paused = (
            await call(
                raw,
                "wot_write_property",
                requestId="decline-credentials",
                contextId=paused["contextId"],
                arguments={"thing_id": "device", "property_name": "level", "value": 40},
            )
        ).structured_content
        # A cancellation reply must not retry even if credentials changed meanwhile.
        credential_saved = True
        declined = await call(
            raw,
            "task.resume",
            taskId=paused["taskId"],
            requestId="credentials-declined",
            replies=[
                {
                    "requestId": paused["pending"][0]["requestId"],
                    "response": {"status": "cancelled"},
                }
            ],
        )
        assert declined.structured_content["status"] == "failed"
        assert actions == [10, 30]


async def test_raw_code_context_and_automatic_panel_persistence(environment, monkeypatch):
    from wotbot.agent.tools.run_code import _code_executor_client
    from wotbot.panels.models import Panel, PanelVersion
    from sqlalchemy import select, func

    import importlib

    panel_module = importlib.import_module("wotbot.agent.tools.create_web_interface")
    monkeypatch.setattr(
        panel_module._code_executor_client,
        "store_web_artifact",
        AsyncMock(return_value="panel.html"),
    )
    monkeypatch.setattr(
        panel_module,
        "_thing_affordances",
        AsyncMock(
            return_value=(
                {"urn:device": {"properties": ["level"], "actions": [], "events": []}},
                [],
            )
        ),
    )
    owner = environment[0]
    sessions = []

    async def execute(*, session_id, code):
        sessions.append(session_id)
        # The executor response formatter is exercised by the real tool.
        return {"success": True, "stdout": "ok", "stderr": "", "result": None, "artifacts": []}

    monkeypatch.setattr(_code_executor_client, "execute", execute)

    async def forbidden(state):
        raise AssertionError("Unexpected assistant invocation")

    async with running(graph_for(forbidden), raw_graph=build_raw_graph(InMemorySaver())) as runtime:
        raw = install_profiles(runtime, monkeypatch, owner)["raw"]
        first = await call(raw, "run_code", requestId="python1", arguments={"code": "x=42"})
        context = first.structured_content["contextId"]
        await call(
            raw, "run_code", requestId="python2", contextId=context, arguments={"code": "print(x)"}
        )
        assert sessions == [context, context]
        panel_args = {
            "title": "MCP generated",
            "html": "<p>Hello MCP</p>",
            "capabilities": [
                {"thing_id": "urn:device", "affordances": ["level"], "ops": ["readProperty"]}
            ],
        }
        created = await call(
            raw,
            "create_web_interface",
            requestId="panel-once",
            contextId=context,
            arguments=panel_args,
        )
        assert not created.is_error, created
        assert created.structured_content["status"] == "completed"
        panels = [
            a["descriptor"] for a in created.structured_content["artifacts"] if "descriptor" in a
        ]
        assert len(panels) == 1
        repeated = await call(
            raw,
            "create_web_interface",
            requestId="panel-once",
            contextId=context,
            arguments=panel_args,
        )
        assert repeated.structured_content["taskId"] == created.structured_content["taskId"]
        with get_session_factory()() as session:
            assert session.scalar(select(func.count()).select_from(Panel)) == 1
            assert session.scalar(select(func.count()).select_from(PanelVersion)) == 1


async def test_explicit_intents_skip_classifier_without_leaking_to_next_call(environment):
    from wotbot.agent.nodes import make_router_node, IntentClassification
    from wotbot.agent.intents import INTENTS

    model = type("SimpleModel", (), {})()
    classifier = AsyncMock(return_value=IntentClassification(intent="chat"))
    model.with_structured_output = lambda schema: type("Classifier", (), {"ainvoke": classifier})()
    router = make_router_node(model, 2000)
    from langchain_core.messages import HumanMessage

    state = {"messages": [HumanMessage(content="hello")], "intent": "control"}
    for name in INTENTS:
        assert (await router(state, {"configurable": {"forced_intent": name}}))["intent"] == name
    classifier.assert_not_awaited()
    assert (await router(state, {"configurable": {}}))["intent"] == "chat"
    classifier.assert_awaited_once()


async def test_tool_catalog_paginates_after_future_additions(environment, monkeypatch):
    from types import SimpleNamespace
    from mcp.types import Tool
    from mcp.shared.exceptions import MCPError

    owner, _, other, *_ = environment
    monkeypatch.setattr("wotbot.mcp.server._request_user", lambda: principal(owner))
    monkeypatch.setattr("wotbot.mcp.server.client_supports_apps", lambda ctx: False)
    raw = MCPToolRuntime(profile="raw", get_runtime=lambda: None, settings=Settings())
    for number in range(65):
        name = f"future.tool.{number}"
        raw.tools[name] = Tool(name=name, input_schema={"type": "object"})
    names, cursor = [], None
    first_cursor = None
    for _ in range(5):
        page = await raw._list_tools(None, SimpleNamespace(cursor=cursor))
        assert len(page.tools) <= 50
        names.extend(tool.name for tool in page.tools)
        cursor = page.next_cursor
        first_cursor = first_cursor or cursor
        if not cursor:
            break
    assert set(names) == set(raw.tools) and len(names) == len(set(names))
    monkeypatch.setattr("wotbot.mcp.server._request_user", lambda: principal(other))
    with pytest.raises(MCPError):
        await raw._list_tools(None, SimpleNamespace(cursor=first_cursor))
