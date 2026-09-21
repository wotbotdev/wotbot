"""Shared document wrapper for generated WoT mini-interfaces.

Both ephemeral panels (the ``create_web_interface`` tool) and pinned panels
(the ``panels`` resource render route) wrap the agent's raw body markup into a
standalone document here. The ``window.wot`` bridge is inlined rather than loaded
via ``<script src>`` so the panel's isolated origin needs no bridge endpoint and
pinned panels remain self-contained.
"""

from __future__ import annotations

import html as html_lib
import json
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def _bridge_source() -> str:
    return resources.files("wotbot.panels").joinpath("wot_bridge.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _checks_source() -> str:
    return resources.files("wotbot.panels").joinpath("panel_checks.js").read_text(encoding="utf-8")


def wrap_panel_document(
    body_html: str, title: str = "", *, data: dict[str, str] | None = None
) -> str:
    """Wrap agent-authored body markup into a full panel document.

    The injected bridge exposes ``window.wot`` for device interaction; the panel
    talks only to the parent via postMessage (see wot_bridge.js). ``window.panelChecks``
    lets the panel declare checks against its own output (see panel_checks.js);
    both are defined before the body so authored markup can use them.
    """
    safe_title = html_lib.escape(title or "Interface")
    # JSON is kept separate from authored HTML and never interpolated as code.
    # Escape '<' even inside JSON strings: HTML parses </script> before JS does.
    encoded = json.dumps(data or {}, ensure_ascii=True).replace("<", "\\u003c")
    data_script = (
        "<script>(() => {const sources = " + encoded + ";"
        "Object.defineProperty(window, 'panelData', {value: Object.freeze({"
        "read(name) {if (!Object.prototype.hasOwnProperty.call(sources, name)) "
        "throw new Error('Unknown panel data attachment: ' + name);"
        "return JSON.parse(sources[name]);},"
        "names() {return Object.keys(sources);}"
        "})});})();</script>\n"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        f"<script>{_bridge_source()}</script>\n"
        f"<script>{_checks_source()}</script>\n"
        f"{data_script}"
        "</head>\n"
        f"<body>\n{body_html}\n</body>\n"
        "</html>\n"
    )
