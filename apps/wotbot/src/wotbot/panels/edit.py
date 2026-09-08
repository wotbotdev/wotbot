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

Requested change:
{instruction}

Current capabilities (JSON):
{capabilities}

Current panel HTML:
{html}
"""


def _extract_updated_panel(
    messages: list[Any],
) -> tuple[str, list[dict[str, Any]]] | None:
    """Pull the latest create_web_interface html (args) + capabilities (result)."""
    calls_by_id = _create_web_interface_calls_by_id(messages)
    result: tuple[str, list[dict[str, Any]]] | None = None
    for message in messages:
        panel = _updated_panel_from_tool_message(message, calls_by_id)
        if panel is not None:
            result = panel
    return result


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


def _updated_panel_from_tool_message(
    message: Any,
    calls_by_id: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]] | None:
    if not isinstance(message, ToolMessage):
        return None
    tool_call = calls_by_id.get(message.tool_call_id)
    if not tool_call:
        return None
    html = (tool_call.get("args") or {}).get("html")
    if not isinstance(html, str) or not html:
        return None
    artifacts = _tool_message_artifacts(message.content)
    if not artifacts:
        return None
    first_artifact = artifacts[0]
    if not isinstance(first_artifact, dict):
        return None
    capabilities = first_artifact.get("capabilities")
    return html, capabilities if isinstance(capabilities, list) else []


def _tool_message_artifacts(content: Any) -> list[Any]:
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return []
    else:
        parsed = content
    artifacts = parsed.get("artifacts") if isinstance(parsed, dict) else None
    return artifacts if isinstance(artifacts, list) else []


async def run_panel_edit(
    *,
    graph: Any,
    checkpointer: Any,
    html: str,
    capabilities: list[dict[str, Any]],
    instruction: str,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Run one agent turn to edit the panel; returns (new_html, capabilities)."""
    prompt = _EDIT_INSTRUCTIONS.format(
        instruction=instruction.strip(),
        capabilities=json.dumps(capabilities, ensure_ascii=True),
        html=html,
    )
    thread_id = f"panel-edit-{uuid.uuid4().hex}"
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        # Edit threads are throwaway; don't leave checkpoints lying around.
        if checkpointer is not None:
            with suppress(Exception):
                await checkpointer.adelete_thread(thread_id)

    messages = state.get("messages", []) if isinstance(state, dict) else []
    return _extract_updated_panel(messages)
