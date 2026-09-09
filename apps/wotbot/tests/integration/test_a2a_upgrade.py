"""Upgrade both historical 0008 schemas with real task, panel and download data."""

import runpy
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from a2a.types import Artifact, ListTasksRequest, Part, Task, TaskState
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from .test_a2a import fake_executor, finish, graph_for, request, running
from wotbot.a2a.artifacts import ArtifactStore
from wotbot.a2a.constants import MCP_APP_MIME_TYPE
from wotbot.a2a.interrupts import describe_interrupt
from wotbot.a2a.models import A2AArtifactRecord, A2AMessageRecord, A2ATaskRecord
from wotbot.a2a.server import install_a2a
from wotbot.a2a.store import TaskStore, request_fingerprint
from wotbot.api_keys.store import create_api_key
from wotbot.core.database import get_session_factory, get_sqlalchemy_engine, init_db
from wotbot.core.settings import Settings
from wotbot.core.time import utc_now
from wotbot.mcp_apps.service import load_pinned_version
from wotbot.panels.service import PanelService
from wotbot.threads.models import Thread
from wotbot.virtual_things.db import VirtualThing

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def upgrade_environment(jobs_integration_environment):
    with get_session_factory()() as session:
        session.execute(sa.text("TRUNCATE a2a_artifacts CASCADE"))
        session.commit()
    yield
    with get_session_factory()() as session:
        session.execute(sa.text("TRUNCATE a2a_artifacts, threads CASCADE"))
        session.commit()


def config():
    return Config(str(files("wotbot") / "alembic.ini"))


def prepare_0008(layout):
    command.downgrade(config(), "0008_add_a2a")
    if layout == "original":
        command.downgrade(config(), "0007_add_thing_origin")
        historical = runpy.run_path(
            str(Path(__file__).with_name("fixtures") / "a2a_original_0008.py")
        )
        with get_sqlalchemy_engine().begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                historical["upgrade"]()
            connection.execute(sa.text("UPDATE alembic_version SET version_num = '0008_add_a2a'"))
    metadata = sa.MetaData()
    metadata.reflect(get_sqlalchemy_engine())
    return metadata.tables


@pytest.mark.parametrize("layout", ["original", "cleanup"])
async def test_upgrade_preserves_pauses_contexts_retry_identity_panels_and_downloads(
    upgrade_environment, layout
):
    with get_session_factory()() as session:
        key, token = create_api_key(
            session, user_id="admin", name="upgrade", scopes=["agent:invoke"]
        )
    owner = key.id
    tables = prepare_0008(layout)
    thread_id, task_id, artifact_id = [str(uuid4()) for _ in range(3)]
    context_id = str(uuid4()) if layout == "original" else thread_id
    virtual_id = f"upgrade-virtual-{layout}"
    now = utc_now()
    original = request("Operate", id="original-message")
    effects = []

    async def node(state):
        answer = interrupt({"kind": "confirmation", "question": "Proceed?"})
        effects.append(answer)
        return {"messages": [AIMessage(content="Done")]}

    graph = graph_for(node)
    graph_config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(
        {"messages": [HumanMessage(content="Operate", id="original-message")]}, graph_config
    )
    snapshot = await graph.aget_state(graph_config)
    pending = [describe_interrupt(i) for i in snapshot.interrupts]
    task = Task(id=task_id, context_id=context_id)
    task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
    task.status.timestamp.FromDatetime(now)
    task.history.add().CopyFrom(original.message)
    task.history[0].task_id, task.history[0].context_id = task_id, context_id
    task.artifacts.append(
        Artifact(
            artifact_id=artifact_id,
            name="old.png",
            parts=[
                Part(
                    url=f"http://localhost:8000/a2a/artifacts/{artifact_id}", media_type="image/png"
                )
            ],
            metadata={"expiresAt": (now + timedelta(days=1)).isoformat()},
        )
    )
    fingerprint = request_fingerprint(MessageToDict(original), legacy=layout == "original")
    with get_sqlalchemy_engine().begin() as connection:
        thread = dict(
            id=thread_id,
            title="Preserved agent",
            kind="a2a",
            visible=False,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        if layout == "cleanup":
            thread["owner_api_key_id"] = owner
        connection.execute(tables["threads"].insert().values(**thread))
        if layout == "original":
            connection.execute(
                tables["a2a_contexts"]
                .insert()
                .values(
                    id=context_id,
                    owner=owner,
                    thread_id=thread_id,
                    active_task_id=task_id,
                    created_at=now,
                )
            )
        row = dict(
            id=task_id,
            context_id=context_id,
            owner=owner,
            state="TASK_STATE_INPUT_REQUIRED",
            payload=MessageToDict(task),
            pending=pending,
            updated_at=now,
            expires_at=now + timedelta(days=30),
        )
        if layout == "cleanup":
            row["applied_messages"] = {"original-message": fingerprint}
        connection.execute(tables["a2a_tasks"].insert().values(**row))
        if layout == "original":
            connection.execute(
                tables["a2a_messages"]
                .insert()
                .values(
                    owner=owner,
                    message_id="original-message",
                    request_hash=fingerprint,
                    task_id=task_id,
                )
            )
        export = dict(
            id=artifact_id,
            task_id=task_id,
            owner=owner,
            name="old.png",
            media_type="image/png",
            metadata={"expiresAt": (now + timedelta(days=1)).isoformat()},
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        export["content" if layout == "original" else "executor_artifact_id"] = (
            b"old image" if layout == "original" else "new.png"
        )
        connection.execute(tables["a2a_artifacts"].insert().values(**export))

    with get_session_factory()() as session:
        panel_service = PanelService(session)
        panel = panel_service.create_panel(
            title="Saved", html="<p>Original</p>", capabilities=[], source_thread_id=thread_id
        )
        versions = panel_service.list_versions(panel["id"])
        version_id = versions["items"][0]["id"]
        panel_service.update_panel(panel["id"], html="<p>Edited</p>")
        session.add(
            VirtualThing(
                id=virtual_id,
                title="Virtual",
                owner_thread_id=thread_id,
                abstract_td={},
                shared_state={"value": 42},
            )
        )
        session.commit()
    panel_artifact_id = str(uuid4())
    with get_sqlalchemy_engine().begin() as connection:
        panel_export = dict(
            id=panel_artifact_id,
            task_id=task_id,
            owner=owner,
            name="Saved",
            media_type=MCP_APP_MIME_TYPE,
            panel_version_id=version_id,
            created_at=now,
            metadata={
                "panelVersionId": version_id,
                "bridgeVersion": 2,
                "descriptor": {"panelId": panel["id"]},
                "subscriptions": [{"id": "sub-old", "cursor": "0-0"}]
                if layout == "original"
                else ["sub-old"],
            },
        )
        if layout == "original":
            panel_export["content"] = b"old wrapper"
        connection.execute(tables["a2a_artifacts"].insert().values(**panel_export))
    init_db()
    init_db()  # Repeated startup must leave identity and content unchanged.
    command.check(config())
    store = TaskStore()
    assert store.get(owner, task_id) == task
    assert store.thread_id(owner, task_id) == thread_id
    replay = store.admit(owner, MessageToDict(original))
    assert not replay.execute and replay.task.id == task_id and replay.thread_id == thread_id
    assert store.list(owner, ListTasksRequest(context_id=context_id))[1] == 1
    from a2a.utils.errors import InvalidParamsError

    with pytest.raises(InvalidParamsError, match="messageId"):
        store.admit(owner, MessageToDict(request("Changed", id="original-message")))
    with get_session_factory()() as session:
        assert session.get(Thread, thread_id).owner_api_key_id == owner
        assert session.get(VirtualThing, virtual_id).owner_thread_id == thread_id
        service = PanelService(session)
        assert service.get_panel(panel["id"], include_html=True)["html"] == "<p>Edited</p>"
    pinned = await load_pinned_version(version_id)
    assert pinned.html == "<p>Original</p>"
    panel_record = await ArtifactStore().get(panel_artifact_id, owner=owner)
    assert panel_record.artifact_metadata["subscriptions"] == ["sub-old"]

    async with fake_executor({"new.png": ("new.png", b"new image", "image/png")}) as executor:
        settings = Settings(code_executor_url=executor)
        async with running(graph, settings=settings) as runtime:
            runtime.owner = owner
            app = FastAPI()
            app.state.a2a_runtime = runtime
            install_a2a(app, settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                response = await client.get(
                    f"/a2a/v1/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
                )
                assert response.status_code == 200
                url = response.json()["artifacts"][0]["parts"][0]["url"]
                downloaded = await client.get(url)
                assert downloaded.status_code == 200
                assert downloaded.content == (
                    b"old image" if layout == "original" else b"new image"
                )
            resumed = await runtime.admit(
                owner,
                request(
                    id="resume",
                    task=task_id,
                    context=context_id,
                    data={"requestId": pending[0]["requestId"], "response": {"approved": True}},
                ),
            )
            completed = await finish(runtime, resumed.task)
            assert completed.status.state == TaskState.TASK_STATE_COMPLETED
            assert completed.context_id == context_id and effects == [{"approved": True}]
            followup = await runtime.admit(owner, request(context=context_id))
            await finish(runtime, followup.task)
            assert followup.thread_id == thread_id and followup.task.context_id == context_id
            with get_session_factory()() as session:
                session.get(A2AArtifactRecord, artifact_id).expires_at = now - timedelta(seconds=1)
                session.commit()
            await runtime.sweep()
            expired = await ArtifactStore().get(artifact_id, owner=owner, include_content=True)
            assert expired.legacy_content is None


async def test_upgrade_blocks_ambiguous_message_ids_without_deleting_tasks(upgrade_environment):
    tables = prepare_0008("cleanup")
    owner = "duplicate-owner"
    now = utc_now()
    requests, task_ids = [], []
    with get_sqlalchemy_engine().begin() as connection:
        for index in range(2):
            thread_id, task_id = str(uuid4()), str(uuid4())
            task_ids.append(task_id)
            incoming = request(
                id="reused-continuation",
                task=task_id,
                context=thread_id,
                data={"requestId": str(uuid4()), "response": {"approved": True}},
            )
            requests.append(MessageToDict(incoming))
            task = Task(id=task_id, context_id=thread_id)
            task.status.state = TaskState.TASK_STATE_COMPLETED
            task.status.timestamp.FromDatetime(now)
            connection.execute(
                tables["threads"]
                .insert()
                .values(
                    id=thread_id,
                    title="Preserved",
                    kind="a2a",
                    visible=False,
                    owner_api_key_id=owner,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            connection.execute(
                tables["a2a_tasks"]
                .insert()
                .values(
                    id=task_id,
                    context_id=thread_id,
                    owner=owner,
                    state="TASK_STATE_COMPLETED",
                    payload=MessageToDict(task),
                    updated_at=now,
                    expires_at=now + timedelta(days=index + 1),
                    applied_messages={"reused-continuation": request_fingerprint(requests[-1])},
                )
            )
    init_db()
    with get_session_factory()() as session:
        assert all(session.get(A2ATaskRecord, task_id) for task_id in task_ids)
        identity = session.get(A2AMessageRecord, (owner, "reused-continuation"))
        assert identity.task_id == task_ids[-1]
        assert identity.request_hash == "conflict"
    from a2a.utils.errors import InvalidParamsError

    for incoming in requests:
        with pytest.raises(InvalidParamsError, match="messageId"):
            TaskStore().admit(owner, incoming)
