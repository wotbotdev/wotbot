"""Which Things the current context may see, for services outside the database.

The RDF service keeps every workspace's Things in one store (one named graph
per Thing). Callers pass it the IDs returned here, and it limits each query to
those graphs.
"""

from __future__ import annotations

from sqlalchemy import select

from wotbot.workspaces.binding import workspaces_enabled
from wotbot.workspaces.context import all_workspaces_active, current_workspace_id


class ThingNotVisibleError(LookupError):
    """The Thing does not exist in the caller's workspace."""


def require_visible_thing(thing_id: str) -> None:
    """Refuse to operate a Thing outside the active workspace.

    The WoT runtime resolves Things across all workspaces with its service
    token, so callers must check visibility before forwarding a Thing ID.
    """
    if not workspaces_enabled() or all_workspaces_active():
        return

    from wotbot.catalog.models import Thing
    from wotbot.core.database import get_session_factory

    with get_session_factory()() as session:
        found = session.scalar(
            select(Thing.id).where(
                Thing.id == thing_id, Thing.workspace_id == current_workspace_id()
            )
        )
    if found is None:
        raise ThingNotVisibleError(f"Thing '{thing_id}' was not found")


def visible_thing_ids() -> list[str] | None:
    """Thing IDs of the active workspace, or None when nothing is restricted."""
    if not workspaces_enabled() or all_workspaces_active():
        return None

    from wotbot.catalog.models import Thing
    from wotbot.core.database import get_session_factory

    with get_session_factory()() as session:
        return list(
            session.scalars(
                select(Thing.id)
                # Explicit on purpose: the ORM filter applies too, but this list
                # is the only thing standing between a query and other workspaces.
                .where(Thing.workspace_id == current_workspace_id())
                .order_by(Thing.id)
            )
        )
