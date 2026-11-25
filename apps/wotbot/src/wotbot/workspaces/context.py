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


@contextmanager
def workspace_scope(workspace_id: str) -> Iterator[str]:
    """Run the enclosed block in ``workspace_id``; scopes nest and restore on exit."""
    token = _current_workspace_id.set(validate_workspace_id(workspace_id))
    try:
        yield workspace_id
    finally:
        _current_workspace_id.reset(token)
