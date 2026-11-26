"""Use-case guidance for the agent, configured per workspace.

The guidance is read from the workspace registry and cached briefly, because
it is needed for every model call while changes are rare. The API process
drops its cache entry on every change; other processes (job worker, voice
agent) see the change after at most ``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import time

from wotbot.workspaces.binding import workspaces_enabled
from wotbot.workspaces.context import current_workspace_id

CACHE_TTL_SECONDS = 30.0

# workspace ID -> (expires at, title, guidance)
_cache: dict[str, tuple[float, str, str]] = {}


def _load(workspace_id: str) -> tuple[str, str]:
    from wotbot.core.database import get_session_factory
    from wotbot.workspaces.models import Workspace

    with get_session_factory()() as session:
        row = session.get(Workspace, workspace_id)
        if row is None:
            return "", ""
        return row.title, row.prompt_extra


def workspace_guidance() -> str:
    """The active workspace's agent guidance as a prompt section, or ""."""
    if not workspaces_enabled():
        return ""
    workspace_id = current_workspace_id()
    now = time.monotonic()
    cached = _cache.get(workspace_id)
    if cached is not None and cached[0] > now:
        _, title, guidance = cached
    else:
        title, guidance = _load(workspace_id)
        _cache[workspace_id] = (now + CACHE_TTL_SECONDS, title, guidance)

    guidance = guidance.strip()
    if not guidance:
        return ""
    heading = f"## Use-case guidance: {title}" if title else "## Use-case guidance"
    return f"{heading}\n{guidance}"


def invalidate_workspace_guidance(workspace_id: str | None = None) -> None:
    """Forget cached guidance for one workspace, or for all of them."""
    if workspace_id is None:
        _cache.clear()
    else:
        _cache.pop(workspace_id, None)
