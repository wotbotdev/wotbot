"""Central workspace filter for SQLAlchemy ORM statements.

Instead of adding a ``WHERE workspace_id = ...`` to every query, a single
``do_orm_execute`` hook adds that criterion to every ORM SELECT, UPDATE and
DELETE that touches a model using the ``WorkspaceScoped`` mixin. New rows get
the active workspace through the column default.

Not covered by this hook, and therefore handled separately:

* textual SQL (``sqlalchemy.text``) and direct psycopg connections,
* objects already held in a session's identity map from an earlier load in a
  different workspace (sessions in this service are short-lived per request).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, event
from sqlalchemy.orm import Mapped, ORMExecuteState, mapped_column, with_loader_criteria

from wotbot.workspaces.context import (
    DEFAULT_WORKSPACE_ID,
    all_workspaces_active,
    current_workspace_id,
)

# Execution option that lets trusted service code (for example the WoT runtime's
# registry reads) see rows of every workspace:
#   session.execute(stmt, execution_options={ALL_WORKSPACES: True})
ALL_WORKSPACES = "wotbot_all_workspaces"


class WorkspaceScoped:
    """Mixin for ORM models whose rows belong to exactly one workspace."""

    workspace_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=current_workspace_id,
        server_default=DEFAULT_WORKSPACE_ID,
        index=True,
    )


def _apply_workspace_criteria(execute_state: ORMExecuteState) -> None:
    # Lazy and column loads inherit the criteria from the statement that loaded
    # the parent, so adding it again would only duplicate the WHERE clause.
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return
    if execute_state.execution_options.get(ALL_WORKSPACES, False) or all_workspaces_active():
        return

    workspace_id = current_workspace_id()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            WorkspaceScoped,
            lambda cls: cls.workspace_id == workspace_id,
            include_aliases=True,
        )
    )


# Marker set on the target itself. ``event.contains`` is not used for this check:
# the event registry keys listeners by ``id()`` of the Session subclass each
# ``sessionmaker`` creates, and once such a class is garbage collected a new
# factory can reuse its id and be reported as already filtered.
_INSTALLED_MARKER = "_wotbot_workspace_filter_installed"


def install_workspace_filter(target: Any) -> None:
    """Attach the workspace filter to a ``sessionmaker`` or ``Session`` (idempotent)."""
    if workspace_filter_installed(target):
        return
    event.listen(target, "do_orm_execute", _apply_workspace_criteria)
    setattr(target, _INSTALLED_MARKER, True)


def workspace_filter_installed(target: Any) -> bool:
    return bool(getattr(target, _INSTALLED_MARKER, False))
