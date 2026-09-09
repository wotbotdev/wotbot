from __future__ import annotations

from importlib.resources import files

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from wotbot.catalog.service import ThingCatalogWriteService
from wotbot.core.database import get_session_factory, get_sqlalchemy_engine
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
    command.upgrade(config, "head")
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
