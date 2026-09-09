from __future__ import annotations

from datetime import timedelta
from importlib.resources import files
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from wotbot.agent_api.models import AgentArtifactRecord
from wotbot.agent_api.store import TaskStore
from wotbot.agent_api.types import TaskState
from wotbot.catalog.service import ThingCatalogWriteService
from wotbot.core.database import get_session_factory, get_sqlalchemy_engine, init_db
from wotbot.core.time import utc_now
from wotbot.discovery.source_models import SourceRecord
from wotbot.discovery.source_store import (
    count_source_dependents,
    delete_source,
    get_source,
    get_source_credential,
    insert_source,
    set_source_credential,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_agent_artifacts(jobs_integration_environment):
    # Task provenance is a soft reference, so the shared fixture's thread
    # truncation does not remove artifacts left by other integration modules.
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE a2a_artifacts"))
        session.commit()


def _alembic_config() -> Config:
    return Config(str(files("wotbot") / "alembic.ini"))


def _source(source_id: str = "urn:wotbot:source:udata:test") -> SourceRecord:
    return SourceRecord(
        id=source_id,
        provider="udata",
        external_id="https://data.example",
        title="Example data",
        description="Example catalog",
        tags=["example"],
        config={"url": "https://data.example"},
        network_access="public",
        security_name="source_sc",
        security_scheme="apikey",
    )


def _resource_td(thing_id: str = "urn:test:resource") -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": "Resource",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
    }


def test_alembic_metadata_has_no_pending_schema_drift(jobs_integration_environment) -> None:
    command.check(_alembic_config())


def test_agent_execution_is_one_revision_after_discovery() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == ["0008_agent_execution"]
    revisions = list(script.iterate_revisions("head", "0007_add_thing_origin"))
    assert [revision.revision for revision in revisions] == ["0008_agent_execution"]


def _agent_request(family):
    request = {"message": {"messageId": "shared-id", "parts": [{"text": "Hello"}]}}
    if family == "raw":
        request["operation"] = {"kind": "raw", "name": "get_current_time", "arguments": {}}
    return request


def test_branch_head_can_be_restamped_without_changing_retained_agent_data() -> None:
    config = _alembic_config()
    store = TaskStore()
    owner = str(uuid4())
    tasks = {
        family: store.admit(owner, _agent_request(family)).task for family in ("assistant", "raw")
    }
    artifact_id = str(uuid4())
    with get_session_factory()() as session:
        session.add(
            AgentArtifactRecord(
                id=artifact_id,
                task_id=tasks["assistant"].id,
                owner=owner,
                name="Retained export",
                media_type="image/png",
                artifact_metadata={"kind": "image"},
                legacy_content=b"retained bytes",
                created_at=utc_now(),
                expires_at=utc_now() + timedelta(days=1),
            )
        )
        session.execute(
            text("UPDATE alembic_version SET version_num = '0011_agent_message_family'")
        )
        session.commit()
    # --purge permits adopting the squashed revision even though the old
    # feature-branch revision is no longer present in the migration graph.
    command.stamp(config, "0008_agent_execution", purge=True)
    init_db()
    init_db()
    command.check(config)
    for family, task in tasks.items():
        assert store.get(owner, task.id, family) == task
        replay = store.admit(owner, _agent_request(family))
        assert not replay.execute and replay.task == task
    with get_session_factory()() as session:
        assert session.get(AgentArtifactRecord, artifact_id).legacy_content == b"retained bytes"


@pytest.mark.parametrize("family", ["assistant", "raw"])
def test_agent_downgrade_preserves_retained_tasks(family) -> None:
    store = TaskStore()
    owner = str(uuid4())
    admitted = store.admit(owner, _agent_request(family))
    # Completed results and their retry identities still require this schema.
    admitted.task.status.state = TaskState.TASK_STATE_COMPLETED
    store.save(owner, admitted.task)
    with pytest.raises(RuntimeError, match="agent execution records remain"):
        command.downgrade(_alembic_config(), "0007_add_thing_origin")
    assert store.get(owner, admitted.task.id, family) == admitted.task
    assert not store.admit(owner, _agent_request(family)).execute
    command.check(_alembic_config())


def test_discovery_source_migration_downgrades_and_upgrades(
    jobs_integration_environment,
) -> None:
    config = _alembic_config()
    command.downgrade(config, "0006_virtual_thing_shared_state")
    inspector = inspect(get_sqlalchemy_engine())
    assert "discovery_sources" not in inspector.get_table_names()
    assert "origin_source_id" not in {column["name"] for column in inspector.get_columns("things")}

    command.upgrade(config, "head")
    inspector = inspect(get_sqlalchemy_engine())
    assert "discovery_sources" in inspector.get_table_names()
    assert "discovery_source_credentials" in inspector.get_table_names()
    assert "origin_source_id" in {column["name"] for column in inspector.get_columns("things")}


def test_source_registry_and_resource_origin_are_separate_and_idempotent(
    jobs_integration_environment,
) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE things, discovery_source_credentials, discovery_sources, "
                "thing_event_outbox CASCADE"
            )
        )
        session.commit()
        source = insert_source(session, _source())
        session.commit()
        assert session.scalar(text("SELECT count(*) FROM things")) == 0
        assert session.scalar(text("SELECT count(*) FROM thing_event_outbox")) == 0
        service = ThingCatalogWriteService(session)
        first, created = service.create_discovered(
            _resource_td(),
            provider="udata",
            external_id="roads",
            source_id=source.id,
        )
        second, created_again = service.create_discovered(
            _resource_td(),
            provider="udata",
            external_id="roads",
            source_id=source.id,
        )
        manual = service.create(
            {
                **_resource_td("urn:test:manual"),
                "origin": {"kind": "discovery", "source_id": "forged"},
                "source": "forged",
            }
        )

    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert first.origin_source_id == source.id
    assert first.origin_provider == "udata"
    assert first.origin_external_id == "roads"
    assert "origin" not in first.document
    assert "source" not in first.document
    assert manual.origin_kind == "manual"


def test_source_identity_foreign_keys_and_credential_cleanup(
    jobs_integration_environment,
) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE things, discovery_source_credentials, discovery_sources, "
                "thing_event_outbox CASCADE"
            )
        )
        session.commit()
        source = insert_source(session, _source())
        set_source_credential(
            session,
            source_id=source.id,
            security_name="source_sc",
            scheme="apikey",
            credentials={"apiKey": "secret"},
        )
        session.commit()

        with pytest.raises(IntegrityError):
            insert_source(session, _source("urn:wotbot:source:udata:duplicate"))
        session.rollback()

        thing, _ = ThingCatalogWriteService(session).create_discovered(
            _resource_td(),
            provider="udata",
            external_id="roads",
            source_id=source.id,
        )
        assert count_source_dependents(session, source.id) == 1
        with pytest.raises(IntegrityError):
            delete_source(session, source.id)
            session.commit()
        session.rollback()

        ThingCatalogWriteService(session).delete(thing.id)
        assert delete_source(session, source.id) is True
        session.commit()
        assert get_source(session, source.id) is None
        assert (
            get_source_credential(
                session,
                source_id=source.id,
                security_name="source_sc",
            )
            is None
        )


def test_a2a_migration_preserves_existing_chat_panels_jobs_and_virtual_ownership(
    jobs_integration_environment,
) -> None:
    from wotbot.core.time import utc_now
    from wotbot.jobs.db import JobRecord, JobRunRecord
    from wotbot.panels.service import PanelService
    from wotbot.threads.store import ThreadStore
    from wotbot.virtual_things.db import VirtualThing

    config = _alembic_config()
    command.downgrade(config, "0007_add_thing_origin")
    store = ThreadStore()
    now = utc_now()
    # Written as the pre-migration schema shaped it: at 0007 `threads` has no
    # owner_api_key_id, so the current ORM model cannot read or write this row.
    stamp = now.isoformat()
    with get_session_factory()() as session:
        session.execute(
            text(
                "INSERT INTO threads (id, title, created_at, updated_at, kind, visible)"
                " VALUES (:id, :title, :created_at, :updated_at, 'chat', true)"
            ),
            {
                "id": "preserved-chat",
                "title": "My conversation",
                "created_at": stamp,
                "updated_at": stamp,
            },
        )
        session.commit()
    chat_id = "preserved-chat"
    with get_session_factory()() as session:
        panel = PanelService(session).create_panel(
            title="Preserved", html="<p>Original</p>", capabilities=[], source_thread_id=chat_id
        )
        PanelService(session).update_panel(panel["id"], html="<p>Edited</p>")
        versions = PanelService(session).list_versions(panel["id"])
        session.add(
            JobRecord(
                id="preserved-job",
                name="Interactive job",
                created_from_thread_id=chat_id,
                job_thread_id="job:preserved",
                action_kind="prompt",
                interaction_mode="required_checkin",
                output_kind="narrative",
                action={"kind": "prompt", "prompt": "Ask first"},
                trigger_kind="time",
                trigger={"kind": "time"},
                output={"kind": "narrative"},
                waiting_question="Proceed?",
                active_run_id="preserved-run",
                last_run_status="waiting_for_input",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            VirtualThing(
                id="preserved-virtual",
                title="Virtual",
                owner_thread_id=chat_id,
                abstract_td={},
                shared_state={"value": 42},
            )
        )
        session.flush()
        session.add(
            JobRunRecord(
                id="preserved-run",
                job_id="preserved-job",
                job_thread_id="job:preserved",
                source="manual",
                status="waiting_for_input",
                result={"question": "Proceed?"},
                started_at=now,
                created_at=now,
            )
        )
        session.commit()
    # The feature migration must preserve unrelated data in both directions,
    # and remain safe to apply again after a clean downgrade.
    for _ in range(2):
        command.upgrade(config, "head")
        command.check(config)
        preserved = store.get(chat_id)
        assert preserved["title"] == "My conversation" and preserved["kind"] == "chat"
        with get_session_factory()() as session:
            service = PanelService(session)
            assert service.list_versions(panel["id"]) == versions
            assert service.get_panel(panel["id"], include_html=True)["html"] == "<p>Edited</p>"
            assert session.get(JobRecord, "preserved-job").waiting_question == "Proceed?"
            assert session.get(JobRecord, "preserved-job").interaction_mode == "required_checkin"
            assert session.get(JobRunRecord, "preserved-run").status == "waiting_for_input"
            assert session.get(VirtualThing, "preserved-virtual").owner_thread_id == chat_id
            assert session.get(VirtualThing, "preserved-virtual").shared_state == {"value": 42}
        command.downgrade(config, "0007_add_thing_origin")
        inspector = inspect(get_sqlalchemy_engine())
        assert not {
            "a2a_tasks",
            "a2a_messages",
            "a2a_artifacts",
            "agent_subscriptions",
        }.intersection(inspector.get_table_names())
        assert not {"owner_api_key_id", "legacy_a2a_context_id"}.intersection(
            column["name"] for column in inspector.get_columns("threads")
        )
    command.upgrade(config, "head")
