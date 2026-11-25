"""Logical workspaces: several isolated use cases in one WoTBot deployment.

Disabled unless ``WORKSPACES_ENABLED=true``. See ``context`` for how the active
workspace is tracked and ``orm`` for the central query filter.
"""

from wotbot.workspaces.context import (
    DEFAULT_WORKSPACE_ID,
    InvalidWorkspaceIdError,
    current_workspace_id,
    validate_workspace_id,
    workspace_scope,
)
from wotbot.workspaces.orm import (
    ALL_WORKSPACES,
    WorkspaceScoped,
    install_workspace_filter,
    workspace_filter_installed,
)

__all__ = [
    "ALL_WORKSPACES",
    "DEFAULT_WORKSPACE_ID",
    "InvalidWorkspaceIdError",
    "WorkspaceScoped",
    "current_workspace_id",
    "install_workspace_filter",
    "validate_workspace_id",
    "workspace_filter_installed",
    "workspace_scope",
]
