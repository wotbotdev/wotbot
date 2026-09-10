"""MCP guidance layered over the shared tool capability descriptions."""

ASSISTANT_RESULT = (
    "Returns a task snapshot with messages, statusMessage and artifacts. "
    "Use task.get while status is submitted or working; use task.resume when input is required."
)
RAW_RESULT = (
    "Returns a task snapshot; the completed tool output is in result. "
    "Use task.get while status is submitted or working."
)
SERVER_INSTRUCTIONS = (
    "Execution tools return task snapshots in structuredContent and as JSON text. "
    "Assistant replies are in messages/statusMessage; raw tool output is in result; "
    "generated outputs are in artifacts. A successful MCP call does not mean execution "
    "has finished: inspect status. For submitted or working, use task.get. For input_required "
    "or auth_required, answer every pending request using task.resume and its responseSchema. "
    "Provision credentials through the WoTBot credential API, never in tool arguments or replies. "
    "A timeout or disconnect leaves work running; task.cancel stops it without undoing completed "
    "actions. Failed tasks may also have performed actions; inspect their outcomes before new work. "
    "Use a new requestId for each invocation or resume, retaining it and the original inputs "
    "for identical retries. Omit contextId for a new conversation; reuse the returned contextId "
    "for later calls that need the same Python session or discovery candidates. A context accepts "
    "one running or paused task at a time. Each API key owns its tasks and contexts. A2A, "
    "assistant and intents share conversations; raw contexts are separate. Artifact downloads "
    "expire: use artifact.get for a fresh link while the file is retained. Generated panels "
    "are saved automatically; their panelUrl opens the WoTBot UI under its existing access controls."
)

_RAW_DESCRIPTIONS = {
    # The raw schema hides the internal conversation filter.
    "list_jobs": "List all automation jobs in this WoTBot deployment.",
}
_RAW_NOTES = {
    "run_code": (
        "Reuse contextId across calls to keep the same Python session. Generated files and "
        "charts appear in the task's artifacts with download links and expiry metadata."
    ),
    "create_web_interface": (
        "On success, the panel is saved automatically. Open the panelUrl in the returned "
        "artifact descriptor through the WoTBot UI."
    ),
    "discover_external": (
        "Reuse the returned contextId when calling onboard_candidate so its candidate IDs "
        "remain available."
    ),
    "onboard_candidate": "Use the same contextId as the discover_external call that found it.",
    "register_external_source": (
        "Execution pauses with a source_registration request. Review the draft, register it "
        "through the source API, then use task.resume with the requested source_id reply, or cancel."
    ),
    "wot_observe_property": (
        "The result includes subscription.subscriptionId and cursor. Use that subscriptionId, "
        "the task's contextId and the initial cursor with subscription.poll; carry nextCursor "
        "forward on later polls. Polling renews a one-hour idle lease."
    ),
    "wot_subscribe_event": (
        "The result includes subscription.subscriptionId and cursor. Use that subscriptionId, "
        "the task's contextId and the initial cursor with subscription.poll; carry nextCursor "
        "forward on later polls. Polling renews a one-hour idle lease."
    ),
    "wot_remove_subscription": (
        "Use the contextId that created the subscription and its returned subscriptionId "
        "as arguments.subscription_id."
    ),
}


def raw_tool_description(name, tool):
    return "\n\n".join(
        part
        for part in (
            _RAW_DESCRIPTIONS.get(name, tool.description),
            _RAW_NOTES.get(name),
            RAW_RESULT,
        )
        if part
    )
