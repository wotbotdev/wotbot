"""Serve the vendored MCP Apps SDK to sandboxed generated panels."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Response

EXT_APPS_VERSION = "1.7.5"
EXT_APPS_FILENAME = f"ext-apps-{EXT_APPS_VERSION}.js"
EXT_APPS_PATH = f"/mcp-apps/{EXT_APPS_FILENAME}"

_STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter(tags=["mcp-apps"])


@lru_cache(maxsize=1)
def ext_apps_bundle() -> bytes:
    return (_STATIC_DIR / EXT_APPS_FILENAME).read_bytes()


def ext_apps_module_url(public_url: str) -> str:
    return f"{public_url.rstrip('/')}{EXT_APPS_PATH}"


@router.get(EXT_APPS_PATH, include_in_schema=False)
async def get_ext_apps_bundle() -> Response:
    """Return the pinned MCP Apps SDK bundle.

    A generated panel runs in an opaque-origin iframe, so its ES module import
    is a cross-origin CORS request that has to be allowed from any origin. The
    file is immutable — its version is in the name — so it caches forever.
    """
    return Response(
        content=ext_apps_bundle(),
        media_type="text/javascript",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        },
    )
