"""LLM-assisted editing of a pinned panel.

Runs a focused turn of the foreground agent graph, seeded with the panel's
current HTML + capabilities and the user's natural-language instruction, and
extracts the updated panel from the agent's ``create_web_interface`` call. The
agent can discover new Things (things_search / wot_get_*) when the edit needs
an affordance the panel doesn't already use, and it re-declares the capability
allowlist for the new version.
"""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

_EDIT_INSTRUCTIONS = """\
You are updating an existing Thing control panel (an interactive HTML/JS
mini-interface). Apply the requested change, then emit the COMPLETE updated panel
using the create_web_interface tool — re-declaring every capability the updated
panel uses. Make the smallest change that satisfies the request and keep the rest
of the panel intact. If the change needs a Thing or affordance the panel does not
already use, discover it first (things_search, wot_get_property, wot_get_action).
Panel JavaScript must treat window.wot.readProperty/writeProperty/invokeAction
results as decoded Thing values directly. Do not access transport wrapper
fields like result, payload, completed_result, or payload.data unless those
fields are explicitly part of the inspected Thing value schema.
Binary values come back as `{{ kind: "binary", contentType, bodyBase64,
sizeBytes }}`; use window.wot.binaryToBlob, binaryToObjectUrl, or binaryToBytes
instead of reading transport envelopes.

Current attached data references (JSON, reuse these IDs in create_web_interface.data):
{data}
Read attached values with window.panelData.read(name). Keep these attachments
unless the requested change removes or replaces them. Do not copy their contents
into HTML; attachment IDs continue to work after the original download expires.

Requested change:
{instruction}

Current capabilities (JSON):
{capabilities}

Current panel HTML:
{html}
"""


@dataclass
class PanelEditResult:
    html: str | None = None
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, str] = field(default_factory=dict)
    browser_validation: dict[str, Any] | None = None


def _extract_edit_result(messages: list[Any]) -> PanelEditResult:
    """Keep evidence with its matching successful edit, or the last failed attempt."""
    calls_by_id = _create_web_interface_calls_by_id(messages)
    updated = None
    failure = PanelEditResult()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_call = calls_by_id.get(message.tool_call_id)
        if not tool_call:
            continue
        result = _tool_result(message.content)
        validation = result.get("browser_validation")
        validation = validation if isinstance(validation, dict) else None
        # A blocked extra call points at the previous attempt's report but has
        # no diagnostics. Retain the actual attempt rather than that placeholder.
        if validation and (validation.get("status") != "blocked" or not failure.browser_validation):
            failure = PanelEditResult(browser_validation=validation)
        if result.get("error") or (validation and validation.get("status") != "passed"):
            continue
        html = (tool_call.get("args") or {}).get("html")
        artifacts = result.get("artifacts")
        if not isinstance(html, str) or not html or not isinstance(artifacts, list):
            continue
        artifact = next(
            (item for item in artifacts if isinstance(item, dict) and item.get("kind") == "web"),
            None,
        )
        if artifact is None:
            continue
        capabilities, data = artifact.get("capabilities"), artifact.get("data")
        updated = PanelEditResult(
            html=html,
            capabilities=capabilities if isinstance(capabilities, list) else [],
            data=data if isinstance(data, dict) else {},
            browser_validation=validation,
        )
    return updated or failure


def _create_web_interface_calls_by_id(messages: list[Any]) -> dict[str, dict[str, Any]]:
    calls_by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                if tool_call.get("name") == "create_web_interface":
                    call_id = tool_call.get("id")
                    if isinstance(call_id, str):
                        calls_by_id[call_id] = tool_call
    return calls_by_id


def _tool_result(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
    else:
        parsed = content
    return parsed if isinstance(parsed, dict) else {}


async def run_panel_edit(
    *,
    graph: Any,
    checkpointer: Any,
    html: str,
    capabilities: list[dict[str, Any]],
    instruction: str,
    data: dict[str, str] | None = None,
) -> PanelEditResult:
    """Run one edit turn and retain its validation feedback, including on failure."""
    prompt = _EDIT_INSTRUCTIONS.format(
        instruction=instruction.strip(),
        capabilities=json.dumps(capabilities, ensure_ascii=True),
        html=html,
        data=json.dumps(data or {}, ensure_ascii=True),
    )
    thread_id = f"panel-edit-{uuid.uuid4().hex}"
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"configurable": {"thread_id": thread_id, "panel_data": data or {}}},
        )
    finally:
        # Edit threads are throwaway; don't leave checkpoints lying around.
        if checkpointer is not None:
            with suppress(Exception):
                await checkpointer.adelete_thread(thread_id)

    messages = state.get("messages", []) if isinstance(state, dict) else []
    return _extract_edit_result(messages)
