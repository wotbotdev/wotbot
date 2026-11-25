"""Bind the workspace of an authenticated principal to the current task."""

from __future__ import annotations

from typing import Protocol

from wotbot.core.config import get_settings
from wotbot.workspaces.context import (
    DEFAULT_WORKSPACE_ID,
    bind_all_workspaces,
    bind_workspace,
)


class Principal(Protocol):
    auth_type: str
    workspace_id: str | None


def workspaces_enabled() -> bool:
    return get_settings().workspaces_enabled


def bind_principal_workspace(principal: Principal) -> None:
    """Make the principal's workspace the active one for the current task.

    API keys carry exactly one workspace. Internal services (WoT runtime,
    virtual servient) read across all workspaces. Does nothing while
    ``WORKSPACES_ENABLED`` is off.
    """
    if not workspaces_enabled():
        return
    if principal.auth_type == "service":
        bind_all_workspaces()
        return
    bind_workspace(principal.workspace_id or DEFAULT_WORKSPACE_ID)
