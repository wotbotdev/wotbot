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
