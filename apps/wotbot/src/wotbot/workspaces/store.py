"""Persistence for the workspace registry."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wotbot.api_keys.models import ApiKey
from wotbot.catalog.models import Thing
from wotbot.core.time import utc_now
from wotbot.discovery.source_models import DiscoverySource
from wotbot.workspaces.context import DEFAULT_WORKSPACE_ID, validate_workspace_id
from wotbot.workspaces.models import Workspace, WorkspaceRecord
from wotbot.workspaces.orm import ALL_WORKSPACES


class WorkspaceConflictError(RuntimeError):
    """Raised when a workspace with the same ID already exists."""


class WorkspaceNotEmptyError(RuntimeError):
    """Raised when deleting a workspace that still owns keys or data."""


def to_record(row: Workspace) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=row.id,
        title=row.title,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
        prompt_extra=row.prompt_extra,
    )


def list_workspaces(session: Session) -> list[WorkspaceRecord]:
    rows = session.scalars(select(Workspace).order_by(Workspace.id)).all()
    return [to_record(row) for row in rows]


def get_workspace(session: Session, workspace_id: str) -> WorkspaceRecord | None:
    row = session.get(Workspace, workspace_id)
    return to_record(row) if row is not None else None


def create_workspace(
    session: Session,
    *,
    workspace_id: str,
    title: str,
    description: str = "",
    prompt_extra: str = "",
) -> WorkspaceRecord:
    now = utc_now()
    row = Workspace(
        id=validate_workspace_id(workspace_id),
        title=title,
        description=description,
        prompt_extra=prompt_extra,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise WorkspaceConflictError(f"Workspace '{workspace_id}' already exists") from exc
    return to_record(row)


def update_workspace(
    session: Session,
    workspace_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    prompt_extra: str | None = None,
) -> WorkspaceRecord | None:
    """Change the given fields; returns None when the workspace does not exist."""
    row = session.get(Workspace, workspace_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if description is not None:
        row.description = description
    if prompt_extra is not None:
        row.prompt_extra = prompt_extra
    row.updated_at = utc_now()
    session.flush()
    return to_record(row)


def _dependents(session: Session, workspace_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model in (
        ("API keys", ApiKey),
        ("Things", Thing),
        ("discovery sources", DiscoverySource),
    ):
        count = session.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == workspace_id),
            execution_options={ALL_WORKSPACES: True},
        )
        if count:
            counts[label] = int(count)
    return counts


def delete_workspace(session: Session, workspace_id: str) -> bool:
    """Delete an empty workspace. Returns False when it does not exist."""
    if workspace_id == DEFAULT_WORKSPACE_ID:
        raise ValueError("The default workspace cannot be deleted")
    row = session.get(Workspace, workspace_id)
    if row is None:
        return False
    dependents = _dependents(session, workspace_id)
    if dependents:
        summary = ", ".join(f"{count} {label}" for label, count in dependents.items())
        raise WorkspaceNotEmptyError(f"Workspace '{workspace_id}' still owns {summary}")
    session.delete(row)
    session.flush()
    return True
