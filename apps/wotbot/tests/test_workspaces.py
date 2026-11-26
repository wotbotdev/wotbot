from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import ForeignKey, Integer, Text, create_engine, delete, func, select, update
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    mapped_column,
    relationship,
    sessionmaker,
)

from wotbot.workspaces import (
    ALL_WORKSPACES,
    DEFAULT_WORKSPACE_ID,
    InvalidWorkspaceIdError,
    WorkspaceScoped,
    all_workspaces_active,
    all_workspaces_scope,
    bind_all_workspaces,
    bind_workspace,
    current_workspace_id,
    install_workspace_filter,
    workspace_filter_installed,
    workspace_scope,
)


class _Base(DeclarativeBase):
    pass


class _Note(WorkspaceScoped, _Base):
    __tablename__ = "workspace_test_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    comments: Mapped[list[_Comment]] = relationship(back_populates="note")


class _Comment(WorkspaceScoped, _Base):
    __tablename__ = "workspace_test_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("workspace_test_notes.id"))
    text: Mapped[str] = mapped_column(Text)
    note: Mapped[_Note] = relationship(back_populates="comments")


class _Setting(_Base):
    """A table without the mixin: never filtered."""

    __tablename__ = "workspace_test_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def _factory(*, filtered: bool) -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if filtered:
        install_workspace_filter(factory)
    return factory


def _add_note(factory: sessionmaker[Session], workspace_id: str, text: str) -> int:
    with workspace_scope(workspace_id), factory() as session:
        note = _Note(text=text)
        session.add(note)
        session.commit()
        return note.id


def _texts(factory: sessionmaker[Session], **execution_options: object) -> list[str]:
    with factory() as session:
        rows = session.scalars(
            select(_Note).order_by(_Note.id),
            execution_options=execution_options,
        )
        return [note.text for note in rows]


@pytest.fixture
def factory() -> sessionmaker[Session]:
    factory = _factory(filtered=True)
    _add_note(factory, "alpha", "a1")
    _add_note(factory, "alpha", "a2")
    _add_note(factory, "beta", "b1")
    return factory


def test_outside_any_scope_the_default_workspace_is_active() -> None:
    assert current_workspace_id() == DEFAULT_WORKSPACE_ID


def test_scopes_nest_and_restore() -> None:
    with workspace_scope("alpha"):
        with workspace_scope("beta"):
            assert current_workspace_id() == "beta"
        assert current_workspace_id() == "alpha"
    assert current_workspace_id() == DEFAULT_WORKSPACE_ID


@pytest.mark.parametrize("value", ["", "Alpha", "-alpha", "a b", "a/b", "x" * 64])
def test_invalid_workspace_ids_are_rejected(value: str) -> None:
    with pytest.raises(InvalidWorkspaceIdError), workspace_scope(value):
        pass


def test_new_rows_take_the_active_workspace(factory: sessionmaker[Session]) -> None:
    default_id = _add_note(factory, DEFAULT_WORKSPACE_ID, "d1")
    with factory() as session:
        note = session.get(_Note, default_id)
        assert note is not None
        assert note.workspace_id == DEFAULT_WORKSPACE_ID


def test_select_only_sees_the_active_workspace(factory: sessionmaker[Session]) -> None:
    with workspace_scope("alpha"):
        assert _texts(factory) == ["a1", "a2"]
    with workspace_scope("beta"):
        assert _texts(factory) == ["b1"]
    assert _texts(factory) == []


def test_get_by_primary_key_does_not_cross_workspaces(factory: sessionmaker[Session]) -> None:
    beta_id = _add_note(factory, "beta", "b2")
    with workspace_scope("alpha"), factory() as session:
        assert session.get(_Note, beta_id) is None


def test_aliased_and_joined_queries_are_filtered(factory: sessionmaker[Session]) -> None:
    with workspace_scope("alpha"), factory() as session:
        note_alias = aliased(_Note)
        texts = session.scalars(select(note_alias.text).order_by(note_alias.id)).all()
        assert texts == ["a1", "a2"]


def test_counts_via_select_from_are_filtered(factory: sessionmaker[Session]) -> None:
    with workspace_scope("alpha"), factory() as session:
        assert session.scalar(select(func.count()).select_from(_Note)) == 2


def test_counts_over_an_orm_subquery_are_filtered(factory: sessionmaker[Session]) -> None:
    with workspace_scope("beta"), factory() as session:
        subquery = select(_Note).where(_Note.text.like("%")).subquery()
        assert session.scalar(select(func.count()).select_from(subquery)) == 1


def test_relationship_loads_are_filtered(factory: sessionmaker[Session]) -> None:
    alpha_id = _add_note(factory, "alpha", "a3")
    with workspace_scope("alpha"), factory() as session:
        session.add(_Comment(note_id=alpha_id, text="mine"))
        session.commit()
    # A row in another workspace pointing at the same parent must stay invisible.
    with workspace_scope("beta"), factory() as session:
        session.add(_Comment(note_id=alpha_id, text="foreign"))
        session.commit()

    with workspace_scope("alpha"), factory() as session:
        note = session.get(_Note, alpha_id)
        assert note is not None
        assert [comment.text for comment in note.comments] == ["mine"]


def test_update_is_limited_to_the_active_workspace(factory: sessionmaker[Session]) -> None:
    with workspace_scope("alpha"), factory() as session:
        session.execute(update(_Note).values(text="changed"))
        session.commit()

    assert _texts(factory, **{ALL_WORKSPACES: True}) == ["changed", "changed", "b1"]


def test_delete_is_limited_to_the_active_workspace(factory: sessionmaker[Session]) -> None:
    with workspace_scope("beta"), factory() as session:
        session.execute(delete(_Note))
        session.commit()

    assert _texts(factory, **{ALL_WORKSPACES: True}) == ["a1", "a2"]


def test_all_workspaces_option_bypasses_the_filter(factory: sessionmaker[Session]) -> None:
    with workspace_scope("alpha"):
        assert _texts(factory, **{ALL_WORKSPACES: True}) == ["a1", "a2", "b1"]


def test_all_workspaces_scope_bypasses_the_filter_and_restores(
    factory: sessionmaker[Session],
) -> None:
    with workspace_scope("alpha"):
        with all_workspaces_scope():
            assert all_workspaces_active()
            assert _texts(factory) == ["a1", "a2", "b1"]
            with workspace_scope("beta"):
                assert not all_workspaces_active()
                assert _texts(factory) == ["b1"]
        assert not all_workspaces_active()
        assert _texts(factory) == ["a1", "a2"]


def test_bindings_stay_inside_their_task() -> None:
    async def run_in(workspace_id: str | None) -> tuple[str, bool]:
        if workspace_id is None:
            bind_all_workspaces()
        else:
            bind_workspace(workspace_id)
        await asyncio.sleep(0)
        thread_view = await asyncio.to_thread(current_workspace_id)
        assert thread_view == current_workspace_id()
        return current_workspace_id(), all_workspaces_active()

    async def main() -> list[tuple[str, bool]]:
        results = await asyncio.gather(
            asyncio.create_task(run_in("alpha")),
            asyncio.create_task(run_in("beta")),
            asyncio.create_task(run_in(None)),
        )
        assert (current_workspace_id(), all_workspaces_active()) == (DEFAULT_WORKSPACE_ID, False)
        return list(results)

    assert asyncio.run(main()) == [
        ("alpha", False),
        ("beta", False),
        (DEFAULT_WORKSPACE_ID, True),
    ]


@pytest.mark.parametrize(
    ("enabled", "auth_type", "workspace_id", "expected"),
    [
        ("true", "api_key", "alpha", ("alpha", False)),
        ("true", "api_key", None, (DEFAULT_WORKSPACE_ID, False)),
        ("true", "service", None, (DEFAULT_WORKSPACE_ID, True)),
        ("false", "api_key", "alpha", (DEFAULT_WORKSPACE_ID, False)),
        ("false", "service", None, (DEFAULT_WORKSPACE_ID, False)),
    ],
)
def test_principal_binding_follows_key_and_flag(
    monkeypatch: pytest.MonkeyPatch,
    enabled: str,
    auth_type: str,
    workspace_id: str | None,
    expected: tuple[str, bool],
) -> None:
    from wotbot.auth.models import User
    from wotbot.core.config import get_settings
    from wotbot.workspaces.binding import bind_principal_workspace

    monkeypatch.setenv("WORKSPACES_ENABLED", enabled)
    get_settings.cache_clear()
    user = User(user_id="u", auth_type=auth_type, workspace_id=workspace_id)

    async def bound() -> tuple[str, bool]:
        bind_principal_workspace(user)
        return current_workspace_id(), all_workspaces_active()

    try:
        assert asyncio.run(bound()) == expected
    finally:
        get_settings.cache_clear()


def test_tables_without_the_mixin_are_not_filtered(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.add(_Setting(value="shared"))
        session.commit()
    with workspace_scope("alpha"), factory() as session:
        assert session.scalars(select(_Setting.value)).all() == ["shared"]


def test_without_the_filter_every_row_is_visible() -> None:
    factory = _factory(filtered=False)
    _add_note(factory, "alpha", "a1")
    _add_note(factory, "beta", "b1")
    with workspace_scope("alpha"):
        assert _texts(factory) == ["a1", "b1"]


def test_installing_twice_registers_one_listener() -> None:
    factory = _factory(filtered=True)
    install_workspace_filter(factory)
    _add_note(factory, "alpha", "a1")
    with workspace_scope("alpha"):
        assert _texts(factory) == ["a1"]


@pytest.mark.parametrize(("enabled", "expected"), [("true", True), ("false", False)])
def test_session_factory_installs_filter_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, enabled: str, expected: bool
) -> None:
    from wotbot.core.config import get_settings
    from wotbot.core.database import get_session_factory, get_sqlalchemy_engine

    monkeypatch.setenv("WORKSPACES_ENABLED", enabled)
    get_settings.cache_clear()
    get_session_factory.cache_clear()
    get_sqlalchemy_engine.cache_clear()
    try:
        assert workspace_filter_installed(get_session_factory()) is expected
    finally:
        get_settings.cache_clear()
        get_session_factory.cache_clear()
        get_sqlalchemy_engine.cache_clear()


# -- search indexing ----------------------------------------------------------


def _lamp_td() -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:test:lamp",
        "title": "Lamp",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
    }


def test_change_events_carry_the_workspace_without_changing_the_td_hash() -> None:
    from wotbot.catalog.events.payloads import build_change_event
    from wotbot.catalog.models import ThingRecord
    from wotbot.catalog.store import hash_document
    from wotbot.thing_indexer.summary_utils import compute_td_hash, normalize_thing_td_payload

    td = _lamp_td()
    record = ThingRecord(
        id=td["id"],
        title="Lamp",
        description="",
        tags=[],
        origin_kind="manual",
        origin_provider=None,
        origin_external_id=None,
        origin_source_id=None,
        document=td,
        document_hash=hash_document(td),
        workspace_id="alpha",
    )
    event = build_change_event("create", record)

    assert event["workspaceId"] == "alpha"
    assert "workspaceId" not in normalize_thing_td_payload(event)
    assert compute_td_hash(event) == record.document_hash


def test_remove_events_carry_the_active_workspace() -> None:
    from wotbot.catalog.events.payloads import build_remove_event

    with workspace_scope("beta"):
        assert build_remove_event("urn:test:lamp")["workspaceId"] == "beta"


@pytest.mark.parametrize(
    ("event_type", "workspace_id", "expected"),
    [
        ("create", "alpha", ("upsert", "alpha")),
        ("update", "beta", ("upsert", "beta")),
        ("remove", "beta", ("remove", "beta")),
        ("create", None, ("upsert", DEFAULT_WORKSPACE_ID)),
    ],
)
def test_indexer_writes_into_the_event_workspace(
    event_type: str, workspace_id: str | None, expected: tuple[str, str]
) -> None:
    from types import SimpleNamespace

    from wotbot.thing_indexer.service import ThingIndexerService

    seen: list[tuple[str, str]] = []

    async def prepare(event, *, thing_id, event_type):
        return thing_id

    async def upsert(prepared):
        await asyncio.sleep(0)
        seen.append(("upsert", await asyncio.to_thread(current_workspace_id)))

    async def remove(thing_id):
        seen.append(("remove", current_workspace_id()))

    indexer = SimpleNamespace(
        _prepare_index_entry=prepare, _upsert_index_entry=upsert, _remove_index_entry=remove
    )
    event = {**_lamp_td(), "eventType": event_type}
    if workspace_id is not None:
        event["workspaceId"] = workspace_id

    asyncio.run(ThingIndexerService.process_event(indexer, event))

    assert seen == [expected]
    assert current_workspace_id() == DEFAULT_WORKSPACE_ID


# -- use-case guidance ----------------------------------------------------------

_GUIDANCE = "## Use-case guidance: Energie\nNenne Verbräuche in kWh."


@pytest.fixture
def guidance_enabled(monkeypatch: pytest.MonkeyPatch):
    from wotbot.core.config import get_settings
    from wotbot.workspaces import prompting

    monkeypatch.setenv("WORKSPACES_ENABLED", "true")
    get_settings.cache_clear()
    prompting.invalidate_workspace_guidance()
    yield prompting
    prompting.invalidate_workspace_guidance()
    get_settings.cache_clear()


def test_guidance_is_cached_until_invalidated(guidance_enabled, monkeypatch) -> None:
    loads: list[str] = []

    def load(workspace_id: str) -> tuple[str, str]:
        loads.append(workspace_id)
        return "Energie", "  Nenne Verbräuche in kWh.  "

    monkeypatch.setattr(guidance_enabled, "_load", load)
    with workspace_scope("energie"):
        first = guidance_enabled.workspace_guidance()
        second = guidance_enabled.workspace_guidance()
        guidance_enabled.invalidate_workspace_guidance("energie")
        guidance_enabled.workspace_guidance()

    assert first == second == _GUIDANCE
    assert loads == ["energie", "energie"]


def test_blank_guidance_adds_nothing(guidance_enabled, monkeypatch) -> None:
    monkeypatch.setattr(guidance_enabled, "_load", lambda workspace_id: ("Energie", "   "))
    with workspace_scope("energie"):
        assert guidance_enabled.workspace_guidance() == ""


def test_disabled_workspaces_never_load_guidance(monkeypatch) -> None:
    from wotbot.core.config import get_settings
    from wotbot.workspaces import prompting

    monkeypatch.setenv("WORKSPACES_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(prompting, "_load", lambda workspace_id: pytest.fail("loaded"))
    try:
        assert prompting.workspace_guidance() == ""
    finally:
        get_settings.cache_clear()


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []

    def bind_tools(self, tools, **kwargs):
        return self

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls.append(list(messages))
        return AIMessage(content="ok")


def _send_through_node(monkeypatch, guidance: str, config=None) -> list:
    from langchain_core.messages import HumanMessage

    from wotbot.agent import nodes

    monkeypatch.setattr(nodes, "workspace_guidance", lambda: guidance)
    llm = _RecordingLLM()
    node = nodes._make_llm_node(llm, tools=[], system_text="static", max_tokens=4000)
    asyncio.run(node({"messages": [HumanMessage(content="Wie hoch ist der Verbrauch?")]}, config))
    return llm.calls[0]


@pytest.mark.parametrize("guidance", [_GUIDANCE, ""])
def test_agent_nodes_append_guidance_to_the_static_prompt(
    guidance_enabled, monkeypatch, guidance: str
) -> None:
    sent = _send_through_node(monkeypatch, guidance)

    expected_system = f"static\n\n{guidance}" if guidance else "static"
    assert [message.content for message in sent] == [
        expected_system,
        "Wie hoch ist der Verbrauch?",
    ]


def test_external_agent_requests_send_exactly_one_leading_system_message(
    guidance_enabled, monkeypatch
) -> None:
    # Qwen-style chat templates reject a system message that is not the first.
    from langchain_core.messages import SystemMessage

    sent = _send_through_node(monkeypatch, _GUIDANCE, {"configurable": {"external_agent": True}})

    assert [isinstance(message, SystemMessage) for message in sent] == [True, False]
    system = sent[0].content
    assert system.startswith("static\n\n")
    assert "external agent through the agent API" in system
    assert _GUIDANCE in system


def test_merging_keeps_prompts_with_a_single_system_message_unchanged() -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from wotbot.agent.nodes import _merge_leading_system_messages

    messages = [SystemMessage(content="static"), HumanMessage(content="hi")]
    assert _merge_leading_system_messages(messages) is messages
