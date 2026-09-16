"""Serves the redacted runtime configuration report.

Gated behind the internal API key, like the other operator-facing endpoints:
even redacted, this enumerates internal service URLs, model names and which
integrations are wired up, which is not something to hand to an anonymous
caller.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from wotbot.core.api_dependencies import verify_internal_api_key
from wotbot.core.config_report import build_config_report
from wotbot.core.settings import Settings

router = APIRouter(prefix="/api", tags=["config"])


def _backend_version() -> str:
    try:
        return package_version("wotbot")
    except PackageNotFoundError:  # pragma: no cover - only when run from a bare tree.
        return "unknown"


def _resolve_settings(request: Request) -> Settings:
    """Prefer the settings the running app loaded over a fresh read.

    A fresh ``Settings()`` would re-read the environment and could disagree with
    what the agent is actually using, which would defeat the point.
    """
    settings = getattr(request.app.state, "agent_settings", None)
    if settings is None:
        settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(status_code=503, detail="Settings are not loaded yet")
    return settings


@router.get("/config", dependencies=[Depends(verify_internal_api_key)])
def get_config(request: Request) -> dict[str, Any]:
    return build_config_report(_resolve_settings(request), version=_backend_version())
