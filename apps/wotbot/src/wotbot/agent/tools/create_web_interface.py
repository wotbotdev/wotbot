"""LangChain tool for generating an interactive HTML/JS mini-interface.

The agent authors plain HTML plus JavaScript that drives Things through the
injected ``window.wot`` bridge (see ``wotbot/panels/wot_bridge.js``). The tool
wraps that markup into a standalone document, persists it as a code artifact,
and returns a ``kind: "web"`` artifact plus the capability allowlist the UI must
enforce. The generated code runs in an isolated-origin sandboxed iframe and can
ONLY reach the declared Thing affordances via the bridge.
"""

import asyncio
import logging
from typing import Annotated

import httpx
from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.exc import SQLAlchemyError

from wotbot.agent.tools.contracts import tool
from wotbot.catalog.ids import decode_thing_id
from wotbot.catalog.service import ThingCatalogQueryService
from wotbot.clients.code_executor import CodeExecutorClient
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.panels.attempts import current_attempt
from wotbot.panels.browser import validate_in_browser
from wotbot.panels.data import resolve_data
from wotbot.panels.evidence import BrowserValidation
from wotbot.panels.render import wrap_panel_document
from wotbot.panels.reports import save_report
from wotbot.panels.validate import validate_external_dependencies, validate_panel
from wotbot.panels.visual_review import review_visuals

_settings = Settings()
_code_executor_client = CodeExecutorClient(_settings)
logger = logging.getLogger(__name__)

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
    problems.extend(await validate_external_dependencies(html))
    problems.extend(
        f"No Thing with id '{thing_id}' is registered, so every call against it "
        "fails. Find the right id with things_search."
        for thing_id in missing
    )
    return problems


@tool
async def create_web_interface(
    html: str,
    config: RunnableConfig,
    capabilities: list[Capability] | None = None,
    title: str = "",
    data: dict[str, str] | None = None,
    runtime: Annotated[ToolRuntime, SkipJsonSchema()] = None,
) -> dict:
    """Create an interactive HTML/JS mini-interface (control panel or dashboard).

    Use this when the user wants a custom UI to monitor or operate Things,
    or an interactive visualization of saved analysis results.
    Attach JSON/GeoJSON exports from run_code using `data`, a mapping of short
    names to returned artifact IDs, e.g. {"areas": "file-...geojson"}.
    Use the artifact's `id` field, not its display `ref` or `filename`.
    In JavaScript read the decoded value synchronously with
    `const areas = window.panelData.read("areas")`; `panelData.names()` lists
    attachments. Each read returns a fresh copy of the original JSON value.
    The backend copies the saved bytes directly; NEVER print, retype, compress,
    or simplify a dataset to put it in HTML. Export with application/json or
    application/geo+json. Up to 8 attachments / 8 MiB combined are supported.
    The result returns reusable panel-data IDs and metadata, not the dataset.
    Reuse these IDs in `data` for subsequent panels or edits; pinned panels keep
    their snapshots even after source downloads expire. Interfaces using only
    attached data may omit capabilities. All actual Thing interactions must
    still be declared. Unknown attachment names throw a visible JS error;
    handle loading/rendering errors in your UI.
    During a saved-panel edit, omitting data preserves its current attachments;
    pass an explicit mapping to replace them or {} to remove all attachments.

    Write plain HTML for `html` (body markup plus a
    <script> with your own JS). Drive Things through the injected `window.wot`
    client:

      await wot.readProperty(thingId, name, { uriVariables })
      await wot.writeProperty(thingId, name, value, { uriVariables })
      await wot.invokeAction(thingId, name, input, { uriVariables })
      const sub = wot.observeProperty(thingId, name, (value) => { ... })
      const sub = wot.subscribeEvent(thingId, name, (data) => { ... })
      wot.unsubscribe(sub)

    The bridge resolves readProperty/writeProperty/invokeAction to the decoded
    WoT value directly, the same shape as run_code's wot.read_property.
    Do NOT read transport wrapper fields such as
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
    make a richer UI: cdn.jsdelivr.net, unpkg.com, cdnjs.cloudflare.com,
    cdn.plot.ly, and fonts.googleapis.com / fonts.gstatic.com — scripts, stylesheets, fonts and
    images all load from them, so a library that ships CSS or icon sprites
    alongside its JS (Leaflet, for one) works. Pin exact versions and use
    paths that exist in that version. Each `<script type="module">` has its own
    scope: names it imports are not visible to any other script, so keep the
    code that uses them in the same module. A bare import such as
    `import * as THREE from "three"` only resolves through a
    `<script type="importmap">` mapping it to a CDN URL, placed before the
    modules that use it; addons that import "three" themselves need that
    mapping too. Maps work too: tiles may come from tile.openstreetmap.org
    (including its a/b/c subdomains) or server.arcgisonline.com (Esri World
    Imagery). Any other image must be a `data:` URI — an
    arbitrary image URL is blocked by CSP because it would be a way to leak
    Thing data off the page. Do not add `integrity` attributes to CDN tags:
    hashes recalled from memory are unreliable, and panel validation rejects
    them. You must NOT use fetch/XHR/WebSocket/sendBeacon — all network egress
    is blocked by CSP; the only way to reach registered Things is `window.wot`.
    Inline your own CSS/JS.

    WebGL and WebXR both work in the panel frame: `navigator.xr` is available
    for immersive-vr and immersive-ar sessions (entering one still needs a user
    gesture, e.g. a button in your markup). Load three.js or similar from the
    CDNs above; any model or texture must come from those CDNs or a `data:` URI.

    Declare every Thing affordance the interface uses in `capabilities`; the UI
    rejects any interaction outside this allowlist. Inspect affordance schemas
    with wot_get_property/wot_get_action first so names and value shapes are
    correct.

    State your own claims as checks. Call the injected `panelChecks` once, at the
    end of your script, whenever the panel or the message you send with it asserts
    something factual about the data -- a ranking, a comparison, an extreme, a
    count -- or whenever a library has to have rendered something for the panel to
    be of any use:

      await panelChecks({
        'map has a rendered layer': () => areas.getLayers().length > 0,
        'ranking is identical at 80m and 100m': () => {
          const wind = panelData.read('wind');
          return rankAt(wind, 80).join() === rankAt(wind, 100).join();
        },
      });

    A check fails by returning false, null, undefined or NaN, or by throwing, and
    the message of anything it throws is reported. Read the attachment back inside
    the check instead of comparing against a number you wrote into the markup:
    the point is to catch a claim the data does not support, and a check that
    restates its own answer cannot do that. Two to five checks is usually right.
    A check that cannot fail is worse than no check at all.
    When asserting equality, throw an Error with expected and actual values on
    mismatch. For canvas charts/maps, inspect the library's layers/data rather
    than counting DOM nodes; canvas markers do not have individual DOM elements.

    For every panel, call panelChecks with at least one assertion
    AFTER initialization and rendering finish. A real browser waits for this
    verdict before delivery, checks for runtime errors and blank output, and
    rejects failed or missing checks. It does not click controls or prove that
    the displayed claims are correct. Live panels may read declared properties
    during validation, with real JSON or binary values and uriVariables. Writes,
    actions and subscriptions are blocked and untested; if initialization attempts
    them, validation is inconclusive. Keep operations that change devices behind
    user controls, and use property reads for initial state. The validator allows
    up to 20 reads with a five-second deadline per read. Validator outages, offline
    devices and timeouts are inconclusive;
    do not repeatedly rewrite working code for an infrastructure failure.
    Each panel gets an initial attempt and at most TWO repair attempts in this
    user turn, across static, data and browser failures. Create panels one at a
    time. Respect retry_allowed in the result: false means stop and explain the
    remaining problem. Inconclusive/unavailable checks stop automatic retries
    immediately. A new user request can try again. Saved reports and screenshots
    remain available to the user for 30 days; image bytes never enter this result.
    A separate visual review annotates screenshots at normal and narrow widths.
    Its findings are advisory: a delivered artifact is final for this request.
    Mention any visual warnings or unavailable review; do not regenerate a panel
    automatically just for these warnings or an unavailable visual reviewer.

    The interface is checked before it is stored: each <script> must parse,
    external dependency URLs must resolve, every literal window.wot call must be
    permitted by `capabilities`, and every declared affordance must exist on the
    Thing. Anything wrong comes back as an error instead of an artifact -- fix it
    and call this tool again with the complete corrected panel.

    Returns a web artifact, its validation coverage and capability allowlist. Describe
    the panel by its title and purpose; how it is opened depends on the calling
    interface.
    """
    attempt = (
        current_attempt(runtime.state.get("messages", []), runtime.tool_call_id)
        if runtime
        else current_attempt([], "")
    )
    if attempt.blocked:
        reasons = {
            "parallel": "Create panels one at a time; wait for the current panel result before submitting another.",
            "exhausted": "Panel repair limit reached (initial attempt plus two repairs). Stop rewriting and explain the remaining errors to the user.",
            "inconclusive": "The previous validation was inconclusive or unavailable. Stop automatic retries and explain the diagnostics; a new user request can try again.",
        }
        return {
            "error": reasons[attempt.blocked],
            "panel_retry": {"counted": False},
            "browser_validation": {
                "status": "blocked",
                **attempt.metadata(),
                "report_id": attempt.previous_reports[-1] if attempt.previous_reports else None,
            },
        }

    document = html

    async def finish(validation: BrowserValidation, artifacts: list | None = None):
        report = {
            **validation.model_dump(),
            **attempt.metadata(retry_allowed=validation.status == "failed" and runtime is not None),
        }
        try:
            report = await save_report(
                title=title,
                document=document,
                thread_id=config.get("configurable", {}).get("thread_id"),
                report=report,
                screenshot_base64=validation.screenshot_base64,
                narrow_screenshot_base64=validation.narrow_screenshot_base64,
            )
        except (SQLAlchemyError, ValueError, OSError):
            logger.exception("Could not save panel validation evidence")
            report = {
                **report,
                "status": "unavailable",
                "retry_allowed": False,
                "diagnostics": [
                    {
                        "kind": "report_storage",
                        "message": "Could not save validation evidence. No panel was delivered.",
                    }
                ],
            }
            artifacts = None
        if artifacts:
            return {"browser_validation": report, "artifacts": artifacts}
        guidance = (
            "Fix the reported problems and submit the complete corrected panel."
            if report["retry_allowed"]
            else "Stop automatic retries and explain the remaining problem to the user."
        )
        diagnostics = "\n".join(item["message"] for item in report.get("diagnostics", []))
        return {
            "error": f"Panel validation {report['status']}; no panel was delivered. {guidance}\n{diagnostics}",
            "browser_validation": report,
        }

    if data is None:
        data = config.get("configurable", {}).get("panel_data", {})
    allowed = _normalize_capabilities(capabilities or [])
    if not allowed and not data:
        return await finish(
            BrowserValidation(
                status="failed",
                diagnostics=[
                    {
                        "kind": "capabilities",
                        "message": (
                            "Declare Thing capabilities or attach saved JSON data. "
                            "An interface needs at least one valid capability or data attachment."
                        ),
                    }
                ],
            )
        )

    problems = await _check_panel(html, allowed)
    if problems:
        return await finish(
            BrowserValidation(
                status="failed",
                diagnostics=[{"kind": "static", "message": problem} for problem in problems],
            )
        )

    try:
        data_refs, contents, metadata = await resolve_data(data, _code_executor_client)
        document = wrap_panel_document(html, title, data=contents)
        validation = await validate_in_browser(document, _settings, capabilities=allowed)
        validation.visual_review = await review_visuals(validation, _settings)
        if validation.status != "passed":
            return await finish(validation)
        filename = await _code_executor_client.store_web_artifact(html=document)
    except ValueError as exc:
        return await finish(
            BrowserValidation(status="failed", diagnostics=[{"kind": "data", "message": str(exc)}])
        )
    except httpx.RequestError:
        return await finish(
            BrowserValidation(
                status="unavailable",
                diagnostics=[
                    {
                        "kind": "executor",
                        "message": "Code executor request failed while loading panel data or storing the interface.",
                    }
                ],
            )
        )
    except httpx.HTTPStatusError as e:
        return await finish(
            BrowserValidation(
                status="unavailable",
                diagnostics=[
                    {
                        "kind": "executor",
                        "message": f"Failed to load panel data or store interface (status {e.response.status_code}).",
                    }
                ],
            )
        )

    return await finish(
        validation,
        [
            {
                "ref": "ui_1",
                "kind": "web",
                "filename": filename,
                "capabilities": allowed,
                "data": data_refs,
                "data_metadata": metadata,
            }
        ],
    )
