"""The workspace a unit of work runs in.

The active workspace lives in a ``ContextVar`` so it follows asyncio tasks and
threads started with ``contextvars.copy_context`` without being passed through
every call. Code that runs outside any explicit scope (startup, workers that
have not been made workspace-aware yet) uses ``DEFAULT_WORKSPACE_ID``, so
existing data and behavior stay in one well-defined workspace.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_WORKSPACE_ID = "default"

# Workspace IDs end up in database values, RDF graph names and URLs, so they are
# restricted to a conservative slug alphabet.
_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_current_workspace_id: ContextVar[str | None] = ContextVar(
    "wotbot_workspace_id",
    default=None,
)
# Trusted internal services (the WoT runtime, the virtual servient) read Things
# of every workspace. They still write into ``current_workspace_id()``.
_all_workspaces: ContextVar[bool] = ContextVar("wotbot_all_workspaces", default=False)


class InvalidWorkspaceIdError(ValueError):
    """Raised when a workspace ID does not match the allowed slug format."""


def validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise InvalidWorkspaceIdError(
            "Workspace IDs must be 1-63 characters of lowercase letters, digits, "
            "'-' or '_', starting with a letter or digit"
        )
    return workspace_id


def current_workspace_id() -> str:
    """Return the active workspace, or ``DEFAULT_WORKSPACE_ID`` outside any scope."""
    return _current_workspace_id.get() or DEFAULT_WORKSPACE_ID


def all_workspaces_active() -> bool:
    """Whether reads in the current context bypass the workspace filter."""
    return _all_workspaces.get()


@contextmanager
def workspace_scope(workspace_id: str) -> Iterator[str]:
    """Run the enclosed block in ``workspace_id``; scopes nest and restore on exit."""
    token = _current_workspace_id.set(validate_workspace_id(workspace_id))
    all_token = _all_workspaces.set(False)
    try:
        yield workspace_id
    finally:
        _all_workspaces.reset(all_token)
        _current_workspace_id.reset(token)


@contextmanager
def all_workspaces_scope() -> Iterator[None]:
    """Let reads in the enclosed block see every workspace."""
    token = _all_workspaces.set(True)
    try:
        yield
    finally:
        _all_workspaces.reset(token)


def bind_workspace(workspace_id: str) -> None:
    """Set the workspace for the rest of the current task, without restoring it.

    Only for task entry points that own their context: an HTTP request task or
    an agent run started with ``asyncio.create_task``. Everything the task
    awaits, and every thread started through ``asyncio.to_thread``, inherits it.
    """
    _current_workspace_id.set(validate_workspace_id(workspace_id))
    _all_workspaces.set(False)


def bind_all_workspaces() -> None:
    """Like ``bind_workspace`` for trusted services that read every workspace."""
    _all_workspaces.set(True)
