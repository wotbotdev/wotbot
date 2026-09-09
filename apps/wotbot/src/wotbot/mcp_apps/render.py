"""Two transports, one generated source: preserve the panel's window.wot API."""

import html as html_lib
import json
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlsplit

from wotbot.mcp_apps.assets import ext_apps_module_url

# Match the current Panels UI's permitted libraries and map tiles.
RESOURCE_DOMAINS = [
    "https://cdn.jsdelivr.net",
    "https://unpkg.com",
    "https://cdnjs.cloudflare.com",
    "https://cdn.plot.ly",
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://tile.openstreetmap.org",
    "https://*.tile.openstreetmap.org",
]


@lru_cache
def _bridge():
    return files("wotbot.mcp_apps").joinpath("wot_bridge.js").read_text()


def panel_ui_metadata(settings, markup: str) -> dict:
    public = urlsplit(settings.registry_public_url)
    origin = f"{public.scheme}://{public.netloc}"
    permissions = {}
    # Requests are optional; the host is allowed to omit any of them.
    if "getUserMedia" in markup or "mediaDevices" in markup:
        permissions = {"camera": {}, "microphone": {}}
    if "clipboard" in markup:
        permissions["clipboardWrite"] = {}
    if "geolocation" in markup:
        permissions["geolocation"] = {}
    return {
        "csp": {
            "resourceDomains": [origin, *RESOURCE_DOMAINS],
            "connectDomains": ["blob:", "data:", *RESOURCE_DOMAINS],
        },
        "permissions": permissions,
    }


def wrap_mcp_app_document(body_html: str, title: str, *, settings) -> str:
    metadata = panel_ui_metadata(settings, body_html)
    resources = " ".join(metadata["csp"]["resourceDomains"])
    connections = " ".join(metadata["csp"]["connectDomains"])
    policy = (
        f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {resources}; "
        f"style-src 'unsafe-inline' {resources}; font-src {resources}; img-src data: blob: {resources}; "
        f"connect-src {connections}; media-src data: blob:; worker-src blob:; "
        "frame-src 'none'; base-uri 'none'; form-action 'none'"
    )
    module = json.dumps(ext_apps_module_url(settings.registry_public_url)).replace("<", "\\u003c")
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{html_lib.escape(policy, quote=True)}">'
        f"<title>{html_lib.escape(title or 'WoTBot panel')}</title>"
        f"<script>{_bridge()}</script>"
        f'<script type="module">import {{ App }} from {module}; window.__wotMcpConnect(App);</script>'
        f"</head><body>{body_html}</body></html>"
    )
