"""LangChain tool for generating an interactive HTML/JS mini-interface.

The agent authors plain HTML plus JavaScript that drives Things through the
injected ``window.wot`` bridge (see ``wotbot/panels/wot_bridge.js``). The tool
wraps that markup into a standalone document, persists it as a code artifact,
and returns a ``kind: "web"`` artifact plus the capability allowlist the UI must
enforce. The generated code runs in an opaque-origin sandboxed iframe and can
ONLY reach the declared Thing affordances via the bridge.
"""

import asyncio

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from wotbot.agent.tools.contracts import tool
from wotbot.catalog.ids import decode_thing_id
from wotbot.catalog.service import ThingCatalogQueryService
from wotbot.clients.code_executor import CodeExecutorClient
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.panels.render import wrap_panel_document
from wotbot.panels.validate import validate_panel

_settings = Settings()
_code_executor_client = CodeExecutorClient(_settings)

# Bridge operations the generated UI may request. Kept in sync with the
# window.wot surface exposed by wotbot/panels/wot_bridge.js.
_ALLOWED_OPS = {
    "readProperty",
    "writeProperty",
    "invokeAction",
    "observeProperty",
    "subscribeEvent",
}


class Capability(BaseModel):
    """Declares which affordances of one Thing the interface may use."""

    thing_id: str = Field(description="Thing id the interface may interact with.")
    affordances: list[str] = Field(
        default_factory=list,
        description="Property/action/event names the interface may touch.",
    )
    ops: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed bridge operations: readProperty, writeProperty, "
            "invokeAction, observeProperty, subscribeEvent."
        ),
    )


def _normalize_capabilities(capabilities: list[Capability]) -> list[dict]:
    normalized: list[dict] = []
    for capability in capabilities:
        ops = [op for op in capability.ops if op in _ALLOWED_OPS]
        if not capability.thing_id or not ops:
            continue
        normalized.append(
            {
                "thingId": capability.thing_id,
                "affordances": [a for a in capability.affordances if a],
                "ops": ops,
            }
        )
    return normalized


async def _thing_affordances(
    thing_ids: list[str],
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Affordance names per declared thing, plus the ids the registry does not have.

    Only a 404 counts as missing: the registry answered and the thing is not
    there. Any other failure (unreachable database, unexpected error) yields
    neither, so `validate_panel` stays silent about those things rather than
    blaming the panel for an outage.
    """

    def run() -> tuple[dict[str, dict[str, list[str]]], list[str]]:
        found: dict[str, dict[str, list[str]]] = {}
        missing: list[str] = []
        session_factory = get_session_factory()
        with session_factory() as session:
            service = ThingCatalogQueryService(session)
            for thing_id in thing_ids:
                try:
                    payload = service.get_owned_thing(decode_thing_id(thing_id))
                except HTTPException as exc:
                    if exc.status_code == 404:
                        missing.append(thing_id)
                    continue
                document = payload.get("document")
                if not isinstance(document, dict):
                    continue
                found[thing_id] = {
                    kind: sorted((document.get(kind) or {}).keys())
                    for kind in ("properties", "actions", "events")
                }
        return found, missing

    try:
        return await asyncio.to_thread(run)
    except Exception:
        return {}, []


async def _check_panel(html: str, allowed: list[dict]) -> list[str]:
    """Everything wrong with this panel that can be found without running it."""
    thing_ids = [capability["thingId"] for capability in allowed]
    affordances, missing = await _thing_affordances(thing_ids)
    problems = validate_panel(html, allowed, affordances)
    problems.extend(
        f"No Thing with id '{thing_id}' is registered, so every call against it "
        "fails. Find the right id with things_search."
        for thing_id in missing
    )
    return problems


@tool
async def create_web_interface(
    html: str,
    capabilities: list[Capability],
    title: str = "",
) -> dict:
    """Create an interactive HTML/JS mini-interface (control panel or dashboard).

    Use this when the user wants a custom UI to monitor or operate Things,
    rather than a static chart. Write plain HTML for `html` (body markup plus a
    <script> with your own JS). Drive Things through the injected `window.wot`
    client:

      await wot.readProperty(thingId, name, { uriVariables })
      await wot.writeProperty(thingId, name, value, { uriVariables })
      await wot.invokeAction(thingId, name, input, { uriVariables })
      const sub = wot.observeProperty(thingId, name, (value) => { ... })
      const sub = wot.subscribeEvent(thingId, name, (data) => { ... })
      wot.unsubscribe(sub)

    The bridge resolves readProperty/writeProperty/invokeAction to the decoded
    WoT value directly, the same shape returned by wot_read_property and
    run_code's wot.read_property. Do NOT read transport wrapper fields such as
    result, payload, completed_result, or payload.data in panel JavaScript.
    Use value.value, value.unit, or other nested fields only when the inspected
    property/action schema says the decoded Thing value itself has those fields.
    Binary payloads resolve to `{ kind: "binary", contentType, bodyBase64,
    sizeBytes }`. Use wot.isBinaryPayload(value), wot.binaryToBytes(value),
    wot.binaryToBlob(value), or wot.binaryToObjectUrl(value) for binary media
    and use wot.binaryFromBase64(...) / wot.binaryFromBytes(...) for binary
    writeProperty or invokeAction inputs.
    observeProperty and subscribeEvent callbacks also receive the decoded event
    value directly.

    You MAY load external libraries (charting, icons, fonts) from these CDNs to
    make a richer UI: cdn.jsdelivr.net, unpkg.com, cdnjs.cloudflare.com, and
    fonts.googleapis.com / fonts.gstatic.com — scripts, stylesheets, fonts and
    images all load from them, so a library that ships CSS or icon sprites
    alongside its JS (Leaflet, for one) works. Maps work too: tiles may come
    from tile.openstreetmap.org. Any other image must be a `data:` URI — an
    arbitrary image URL is blocked by CSP because it would be a way to leak
    Thing data off the page. You must NOT use fetch/XHR/WebSocket/sendBeacon —
    all network egress is blocked by CSP; the only way to reach registered Things is
    `window.wot`. Inline your own CSS/JS.

    WebGL and WebXR both work in the panel frame: `navigator.xr` is available
    for immersive-vr and immersive-ar sessions (entering one still needs a user
    gesture, e.g. a button in your markup). Load three.js or similar from the
    CDNs above; any model or texture must come from those CDNs or a `data:` URI.

    Declare every Thing affordance the interface uses in `capabilities`; the UI
    rejects any interaction outside this allowlist. Inspect affordance schemas
    with wot_get_property/wot_get_action first so names and value shapes are
    correct.

    The interface is checked before it is stored: each <script> must parse, every
    literal window.wot call must be permitted by `capabilities`, and every
    declared affordance must exist on the Thing. Anything wrong comes back as an
    error instead of an artifact -- fix it and call this tool again with the
    complete corrected panel.

    The frontend renders the interface below the tool call. Refer to it
    naturally ("the panel above") and never mention raw filenames.
    """
    allowed = _normalize_capabilities(capabilities)
    if not allowed:
        return {
            "error": (
                "Refusing to create an interface with no valid capabilities. "
                "Declare at least one thing_id with allowed ops."
            )
        }

    problems = await _check_panel(html, allowed)
    if problems:
        return {
            "error": (
                "This interface would not work as written, so it was not created:\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + "\nFix these and call create_web_interface again with the "
                "complete corrected panel."
            )
        }

    document = wrap_panel_document(html, title)
    try:
        filename = await _code_executor_client.store_web_artifact(html=document)
    except httpx.ConnectError:
        return {"error": "Code executor service is unavailable. Please try again later."}
    except httpx.TimeoutException:
        return {"error": "Code executor request timed out while storing the interface."}
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to store interface (status {e.response.status_code})."}

    return {
        "artifacts": [
            {
                "ref": "ui_1",
                "kind": "web",
                "filename": filename,
                "capabilities": allowed,
            }
        ]
    }
