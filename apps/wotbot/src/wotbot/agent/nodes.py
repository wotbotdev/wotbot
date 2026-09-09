"""Node helpers for the WoTBot agent graph."""

from __future__ import annotations

from wotbot.agent.intents import IntentId

import json
import logging
from collections.abc import Sequence
from typing import Any, NotRequired, Optional, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, MessagesState
from langgraph.types import Command
from pydantic import BaseModel, Field

from wotbot.agent.camera_context import attach_latest_camera_frame
from wotbot.agent.device_interactions import (
    without_device_interaction_summary_messages,
)
from wotbot.agent.prompts import (
    ANALYSIS_PROMPT,
    CONTROL_PROMPT,
    DISCOVERY_PROMPT,
    JOBS_PROMPT,
    RESPOND_PROMPT,
    ROUTER_PROMPT,
    VIRTUAL_THINGS_PROMPT,
)
from wotbot.core.reasoning_effort import reasoning_effort_kwargs
from wotbot.core.settings import ReasoningEffortSettings, ReasoningEffortStyle
from wotbot.core.time import utc_now
from wotbot.jobs.enums import JobOutputKind

logger = logging.getLogger(__name__)

# A run tagged "nostream" still executes, but LangGraph omits its tokens from
# "messages" stream mode. This keeps internal classification calls out of the
# user-visible answer stream.
NOSTREAM_TAG = "nostream"

BACKGROUND_JOB_PROMPT = """\
You are executing one background prompt job run for WoTBot.

Follow the runtime instructions in the user message. This is not a foreground
conversation about creating or managing jobs; it is the job execution itself.

If the run needs human input, call ask_job_user with one concise question and
then stop. When the user has replied, use that answer to finish the same run.
Do not ask the same question again unless the answer is unusable or required
information is still missing.

For structured record jobs, call submit_job_record once the available data
matches the provided JSON Schema. Do not claim success before submit_job_record
returns ok=true.

When creating plots or charts with run_code, trigger artifact capture explicitly:
call plt.show() for Matplotlib figures or fig.show() for Plotly figures. Do not
only create a figure object and describe it.

Keep final responses concise and factual.
"""


class WotbotState(MessagesState):
    intent: str
    # Set by the route_to handoff tool to request continuation in another
    # branch; consumed and cleared by the dispatch node. Absent/None means the
    # turn ends normally. Only used when agent_handoff_enabled is set.
    next: NotRequired[Optional[str]]
    # Submitted as plain graph state by the chat UI. Only honored when it
    # matches the operator-configured allow-list; see _resolve_reasoning_effort.
    reasoning_effort: NotRequired[Optional[str]]


class IntentClassification(BaseModel):
    intent: IntentId = Field(description="The classified intent")


def _strip_ui_tool_data(message: BaseMessage) -> BaseMessage:
    """Remove UI-only tool data from the copy sent to the LLM.

    ``wot_calls`` are only needed by the UI to render device-interaction
    summaries. File storage IDs and URIs are used by download cards, but are
    not browser links the model should include in its answer. These fields
    stay in the persisted graph state so the frontend still receives them.
    """
    if not isinstance(message, ToolMessage):
        return message
    content = message.content
    if not isinstance(content, str):
        return message
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return message
    if not isinstance(parsed, dict):
        return message
    stripped = {k: v for k, v in parsed.items() if k != "wot_calls"}
    if message.name == "run_code" and isinstance(parsed.get("artifacts"), list):
        stripped["artifacts"] = [
            {k: v for k, v in artifact.items() if k not in {"id", "uri", "content_uri"}}
            if isinstance(artifact, dict) and artifact.get("kind") == "file"
            else artifact
            for artifact in parsed["artifacts"]
        ]
    if stripped == parsed:
        return message
    return message.model_copy(update={"content": json.dumps(stripped)})


def _count_tokens(messages: Sequence[BaseMessage]) -> int:
    """Token counter used for prompt-budget trimming.

    ``count_tokens_approximately``'s char-based heuristic assumes generic
    English-text token/char ratios, which don't match whatever model is
    actually serving the request (e.g. Qwen via vLLM, not OpenAI).
    ``use_usage_metadata_scaling`` calibrates the estimate against the real
    ``usage_metadata.total_tokens`` the model itself already reported on its
    most recent response in this conversation, so counts track the actual
    tokenizer in use. It degrades gracefully to the plain heuristic when no
    usage metadata is available yet (e.g. the first turn, or in tests).
    """
    return count_tokens_approximately(list(messages), use_usage_metadata_scaling=True)


def _group_conversation_units(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    """Split an already-sanitized conversation into atomic eviction units.

    A unit is either one ordinary message, or an AIMessage with tool_calls
    together with every one of its matching ToolMessages. Trimming (below)
    only ever drops or keeps a whole unit, so it can no longer cut through the
    middle of a tool call/result pair -- there's nothing left to repair after.
    This relies on ``_sanitize_message_sequence`` having already run so every
    AIMessage.tool_calls entry here is guaranteed to have a matching
    ToolMessage immediately following it.
    """
    units: list[list[BaseMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_call_ids = {tool_call["id"] for tool_call in message.tool_calls}
            group: list[BaseMessage] = [message]
            next_index = index + 1
            while next_index < len(messages):
                candidate = messages[next_index]
                if not isinstance(candidate, ToolMessage) or candidate.tool_call_id not in (
                    tool_call_ids
                ):
                    break
                group.append(candidate)
                next_index += 1
            units.append(group)
            index = next_index
            continue
        units.append([message])
        index += 1
    return units


def _fit_units_to_budget(units: list[list[BaseMessage]], max_tokens: int) -> list[BaseMessage]:
    """Keep whole units, most-recent first, until the token budget runs out.

    The most recent unit is always kept even if it alone exceeds the budget,
    so a tight budget never returns nothing. Some model chat templates
    (Qwen3.5's, notably, served via vLLM) also reject a request that's
    entirely tool-call/tool-response content with no user-authored message
    anywhere in it ("No user query found in messages."), so the unit holding
    the current turn's HumanMessage is always kept too, regardless of budget.
    """
    keep = [False] * len(units)
    budget = max_tokens
    any_kept = False
    for index in range(len(units) - 1, -1, -1):
        cost = _count_tokens(units[index])
        if not any_kept or cost <= budget:
            keep[index] = True
            any_kept = True
            budget -= cost
        else:
            break

    human_index = next(
        (
            index
            for index in range(len(units) - 1, -1, -1)
            if isinstance(units[index][0], HumanMessage)
        ),
        None,
    )
    if human_index is not None:
        keep[human_index] = True

    result = [message for index, unit in enumerate(units) if keep[index] for message in unit]
    if result and isinstance(result[0], SystemMessage):
        result.pop(0)
    return result


def _trim_conversation(messages: Sequence[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    # Strip ``wot_calls`` BEFORE counting tokens: those device-interaction
    # payloads are removed from the prompt anyway, but a single run_code result
    # can carry megabytes of them. Trimming on the un-stripped messages let that
    # invisible data consume the whole budget and evict the real conversation.
    prepared = [
        _strip_ui_tool_data(message)
        for message in without_device_interaction_summary_messages(messages)
    ]
    # Repair tool_call/ToolMessage pairing BEFORE grouping into eviction units
    # (below), so every unit is already internally valid going in -- trimming
    # can then only ever drop or keep a whole unit, never cut through one.
    sanitized = _sanitize_message_sequence(prepared)
    units = _group_conversation_units(sanitized)
    return _fit_units_to_budget(units, max_tokens)


def _sanitize_message_sequence(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Ensure every AI tool_call has a matching ToolMessage and vice-versa."""
    sanitized: list[BaseMessage] = []
    index = 0

    while index < len(messages):
        message = messages[index]

        # AI message with tool calls: collect all following ToolMessages that match.
        if isinstance(message, AIMessage) and message.tool_calls:
            tool_messages: list[ToolMessage] = []
            next_index = index + 1
            while next_index < len(messages):
                next_message = messages[next_index]
                if not isinstance(next_message, ToolMessage):
                    break
                tool_messages.append(next_message)
                next_index += 1

            if not tool_messages:
                # No tool results at all — drop the AI tool-call message.
                index += 1
                continue

            # Keep only tool_calls that have a matching ToolMessage.
            matched_ids = {tm.tool_call_id for tm in tool_messages}
            matched_calls = [tc for tc in message.tool_calls if tc["id"] in matched_ids]

            if not matched_calls:
                index = next_index
                continue

            # Patch the AI message to only reference matched calls.
            sanitized_message = message
            if len(matched_calls) != len(message.tool_calls):
                additional_kwargs = dict(message.additional_kwargs)
                if additional_kwargs.get("tool_calls"):
                    additional_kwargs["tool_calls"] = [
                        tool_call
                        for tool_call in additional_kwargs["tool_calls"]
                        if tool_call.get("id") in matched_ids
                    ]
                sanitized_message = message.model_copy(
                    update={
                        "tool_calls": matched_calls,
                        "additional_kwargs": additional_kwargs,
                    }
                )

            matched_tool_messages = [tm for tm in tool_messages if tm.tool_call_id in matched_ids]
            sanitized.append(sanitized_message)
            sanitized.extend(matched_tool_messages)
            index = next_index
            continue

        # Orphaned ToolMessage without preceding AI tool call — drop it.
        if isinstance(message, ToolMessage):
            index += 1
            continue

        sanitized.append(message)
        index += 1

    return sanitized


def _make_router_messages(messages: Sequence[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    trimmed = trim_messages(
        without_device_interaction_summary_messages(messages),
        max_tokens=max_tokens,
        token_counter=_count_tokens,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    sanitized = _sanitize_message_sequence(trimmed)
    conversational = [
        message
        for message in sanitized
        if not isinstance(message, ToolMessage)
        and not (isinstance(message, AIMessage) and message.tool_calls)
    ]
    tail = conversational[-3:]

    if not any(isinstance(message, HumanMessage) for message in tail):
        summary = "\n".join(
            f"{message.type}: {message.content}" for message in tail if message.content
        )
        tail = [HumanMessage(content=summary or "Classify the latest request.")]

    return tail


def _latest_run_code_source(messages: Sequence[BaseMessage]) -> str | None:
    """Return the source of the most recent run_code tool call, if any.

    The virtual_things branch has no run_code tool, and a large analysis result
    is usually trimmed out of its context window. Pinning the source lets the
    model reuse prior modelling logic instead of re-deriving it from scratch.
    """
    for message in reversed(messages):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        for tool_call in message.tool_calls:
            if tool_call.get("name") != "run_code":
                continue
            args = tool_call.get("args") or {}
            source = args.get("code") if isinstance(args, dict) else None
            if isinstance(source, str) and source.strip():
                return source.strip()
    return None


def _prior_analysis_block(messages: Sequence[BaseMessage]) -> str:
    source = _latest_run_code_source(messages)
    if not source:
        return ""
    return f"\n\n## Prior Analysis Code\n```python\n{source}\n```"


def _current_time_block() -> str:
    now = utc_now()
    ts_ms = int(now.timestamp() * 1000)
    ts_s = int(now.timestamp())
    return (
        f"\n\n## Current Time\n"
        f"Copy-paste these values directly into run_code. Do NOT reconstruct them with datetime.\n"
        f'- now_iso = "{now.isoformat()}"\n'
        f"- now_ts_s = {ts_s}\n"
        f"- now_ts_ms = {ts_ms}"
    )


def _make_node_prompt(system_text: str, max_tokens: int):
    system_message = SystemMessage(content=system_text)

    def prompt(state: WotbotState) -> list[BaseMessage]:
        trimmed = _trim_conversation(state["messages"], max_tokens)
        return [system_message, *trimmed]

    return prompt


def _active_tools_for_config(tools: list[Any], config: Optional[RunnableConfig]) -> list[Any]:
    if not tools:
        return []

    configurable = config.get("configurable", {}) if config else {}
    return [
        tool
        for tool in tools
        if (
            getattr(tool, "name", None) != "submit_job_record"
            or configurable.get("job_output_kind") == JobOutputKind.STRUCTURED_RECORD.value
        )
    ]


def _thread_id_from_config(config: Optional[RunnableConfig]) -> str | None:
    configurable = config.get("configurable", {}) if config else {}
    thread_id = configurable.get("thread_id") or configurable.get("threadId")
    return thread_id if isinstance(thread_id, str) and thread_id else None


async def _with_camera_context(
    messages: list[BaseMessage],
    *,
    config: Optional[RunnableConfig],
    camera_frames_enabled: bool,
) -> list[BaseMessage]:
    if config and (
        config.get("configurable", {}).get("external_agent")
        or config.get("configurable", {}).get("a2a")
    ):
        messages = [
            messages[0],
            SystemMessage(
                content=(
                    "This request comes from an external agent through the agent API, with no chat UI. "
                    "Use the normal tools and router. Generated outputs are exported as artifacts; "
                    "panels are saved automatically and include a panel link. "
                    "Explain results independently of those outputs. Do not refer to a panel above, "
                    "a download button, or invent URLs. Credentials are provisioned through the "
                    "existing credential API and must never be requested in conversation content."
                )
            ),
            *messages[1:],
        ]
    if not camera_frames_enabled:
        return messages
    attachment = await attach_latest_camera_frame(
        messages,
        thread_id=_thread_id_from_config(config),
    )
    return attachment.messages


def _tool_names(tools: list[Any]) -> list[str]:
    return sorted(str(name) for tool in tools if (name := getattr(tool, "name", None)))


def _resolve_reasoning_effort(
    state: WotbotState, reasoning_effort: ReasoningEffortSettings | None
) -> str | None:
    """Read ``state["reasoning_effort"]`` (set by the chat UI)

    and return it only when it's one of the operator-configured allowed
    levels. Returns ``None`` (provider default) when the feature is disabled
    (``reasoning_effort`` is ``None``), unset, or the value isn't recognized.
    """
    if reasoning_effort is None:
        return None
    value = state.get("reasoning_effort")
    return value if isinstance(value, str) and value in reasoning_effort.levels else None


def _bind_runnable(
    llm: ChatOpenAI,
    active_tools: list[Any],
    *,
    parallel_tool_calls: bool,
    reasoning_effort: str | None,
    reasoning_effort_style: ReasoningEffortStyle = "openai",
):
    bind_kwargs: dict[str, Any] = (
        reasoning_effort_kwargs(reasoning_effort, reasoning_effort_style)
        if reasoning_effort
        else {}
    )
    if active_tools:
        return llm.bind_tools(active_tools, parallel_tool_calls=parallel_tool_calls, **bind_kwargs)
    return llm.bind(**bind_kwargs) if bind_kwargs else llm


def _log_branch_entry(
    branch: str,
    *,
    config: Optional[RunnableConfig],
    active_tools: list[Any],
    parallel_tool_calls: bool,
    reasoning_effort: str | None = None,
) -> None:
    names = _tool_names(active_tools)
    logger.debug(
        "Agent branch entered branch=%s thread_id=%s tool_count=%d tools=%s "
        "parallel_tool_calls=%s reasoning_effort=%s",
        branch,
        _thread_id_from_config(config),
        len(names),
        names,
        parallel_tool_calls,
        reasoning_effort,
    )


def make_router_node(llm: ChatOpenAI, max_tokens: int):
    """Classify the current request into a single graph branch."""
    structured_llm = llm.with_structured_output(IntentClassification)
    system_message = SystemMessage(content=ROUTER_PROMPT)

    async def router(state: WotbotState, config: Optional[RunnableConfig] = None):
        forced = (config or {}).get("configurable", {}).get("forced_intent")
        if forced is not None:
            return {"intent": IntentId(forced).value}
        tail = _make_router_messages(state["messages"], max_tokens)
        router_config = dict(config or {})
        tags = list(router_config.get("tags") or [])
        if NOSTREAM_TAG not in tags:
            tags.append(NOSTREAM_TAG)
        router_config["tags"] = tags
        result = cast(
            IntentClassification,
            await structured_llm.ainvoke(
                [system_message, *tail],
                config=cast(RunnableConfig, router_config),
            ),
        )
        logger.info(
            "Router classified intent thread_id=%s intent=%s",
            _thread_id_from_config(config),
            result.intent,
        )
        return {"intent": result.intent}

    return router


def _prepare_branch_runnable(
    llm: ChatOpenAI,
    tools: list[Any],
    *,
    state: WotbotState,
    config: RunnableConfig | None,
    parallel_tool_calls: bool,
    branch_name: str,
    reasoning_effort: ReasoningEffortSettings | None,
):
    """Resolve active tools + reasoning effort for one node invocation, log

    the branch entry, and return the bound runnable. Shared by every
    LLM-calling node body (respond/control/analysis/jobs/virtual_things/
    background_job).
    """
    active_tools = _active_tools_for_config(tools, config)
    resolved_effort = _resolve_reasoning_effort(state, reasoning_effort)
    _log_branch_entry(
        branch_name,
        config=config,
        active_tools=active_tools,
        parallel_tool_calls=parallel_tool_calls,
        reasoning_effort=resolved_effort,
    )
    return _bind_runnable(
        llm,
        active_tools,
        parallel_tool_calls=parallel_tool_calls,
        reasoning_effort=resolved_effort,
        reasoning_effort_style=reasoning_effort.style if reasoning_effort else "openai",
    )


def _make_llm_node(
    llm: ChatOpenAI,
    *,
    tools: list[Any],
    system_text: str,
    max_tokens: int,
    parallel_tool_calls: bool = True,
    branch_name: str = "llm",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
):
    prompt = _make_node_prompt(system_text, max_tokens)

    # NOTE: keep ``config`` typed as ``Optional[RunnableConfig]``. With
    # ``from __future__ import annotations`` the annotation is a string, and
    # LangGraph only injects the runtime config when it reads as
    # ``"RunnableConfig"`` or ``"Optional[RunnableConfig]"``. Writing it as
    # ``RunnableConfig | None`` silently disables injection, so ``config`` (and
    # the ``thread_id`` needed for live camera frames) arrives as ``None``.
    async def node(state: WotbotState, config: Optional[RunnableConfig] = None):
        runnable = _prepare_branch_runnable(
            llm,
            tools,
            state=state,
            config=config,
            parallel_tool_calls=parallel_tool_calls,
            branch_name=branch_name,
            reasoning_effort=reasoning_effort,
        )
        messages = await _with_camera_context(
            prompt(state),
            config=config,
            camera_frames_enabled=camera_frames_enabled,
        )
        response = await runnable.ainvoke(messages)
        return {"messages": [response]}

    return node


def make_respond_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    return _make_llm_node(
        llm,
        tools=tools,
        system_text=RESPOND_PROMPT + response_instructions,
        max_tokens=max_tokens,
        parallel_tool_calls=parallel_tool_calls,
        branch_name="respond",
        reasoning_effort=reasoning_effort,
        camera_frames_enabled=camera_frames_enabled,
    )


def make_control_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    handoff_note: str = "",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    return _make_llm_node(
        llm,
        tools=tools,
        system_text=CONTROL_PROMPT + handoff_note + response_instructions,
        max_tokens=max_tokens,
        parallel_tool_calls=parallel_tool_calls,
        branch_name="control",
        reasoning_effort=reasoning_effort,
        camera_frames_enabled=camera_frames_enabled,
    )


def make_analysis_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    handoff_note: str = "",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    # Built once: a system prompt that is byte-identical on every call keeps the
    # whole prompt prefix -- history and any attached camera frame included --
    # eligible for provider prefix caching. The current time is deliberately NOT
    # inlined here; it comes from the get_current_time tool, whose result lands
    # in history as an append-only message instead of rewriting the prefix.
    system_message = SystemMessage(content=ANALYSIS_PROMPT + handoff_note + response_instructions)

    # ``config`` typing must stay ``Optional[RunnableConfig]``; see _make_llm_node.
    async def node(state: WotbotState, config: Optional[RunnableConfig] = None):
        trimmed = _trim_conversation(state["messages"], max_tokens)
        messages = await _with_camera_context(
            [system_message, *trimmed],
            config=config,
            camera_frames_enabled=camera_frames_enabled,
        )
        runnable = _prepare_branch_runnable(
            llm,
            tools,
            state=state,
            config=config,
            parallel_tool_calls=parallel_tool_calls,
            branch_name="analysis",
            reasoning_effort=reasoning_effort,
        )
        response = await runnable.ainvoke(messages)
        return {"messages": [response]}

    return node


def make_jobs_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    handoff_note: str = "",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    # Static system prompt; see make_analysis_node for why the time is a tool.
    system_message = SystemMessage(content=JOBS_PROMPT + handoff_note + response_instructions)

    # ``config`` typing must stay ``Optional[RunnableConfig]``; see _make_llm_node.
    async def node(state: WotbotState, config: Optional[RunnableConfig] = None):
        trimmed = _trim_conversation(state["messages"], max_tokens)
        messages = await _with_camera_context(
            [system_message, *trimmed],
            config=config,
            camera_frames_enabled=camera_frames_enabled,
        )
        runnable = _prepare_branch_runnable(
            llm,
            tools,
            state=state,
            config=config,
            parallel_tool_calls=parallel_tool_calls,
            branch_name="jobs",
            reasoning_effort=reasoning_effort,
        )
        response = await runnable.ainvoke(messages)
        return {"messages": [response]}

    return node


def make_virtual_things_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    handoff_note: str = "",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    # Static except for the prior-analysis block, which is derived from state and
    # only changes when new run_code output lands. See make_analysis_node for why
    # the current time is a tool rather than an inlined block.
    system_prefix = VIRTUAL_THINGS_PROMPT + handoff_note + response_instructions

    # ``config`` typing must stay ``Optional[RunnableConfig]``; see _make_llm_node.
    async def node(state: WotbotState, config: Optional[RunnableConfig] = None):
        system_message = SystemMessage(
            content=system_prefix + _prior_analysis_block(state["messages"])
        )
        trimmed = _trim_conversation(state["messages"], max_tokens)
        messages = await _with_camera_context(
            [system_message, *trimmed],
            config=config,
            camera_frames_enabled=camera_frames_enabled,
        )
        runnable = _prepare_branch_runnable(
            llm,
            tools,
            state=state,
            config=config,
            parallel_tool_calls=parallel_tool_calls,
            branch_name="virtual_things",
            reasoning_effort=reasoning_effort,
        )
        response = await runnable.ainvoke(messages)
        return {"messages": [response]}

    return node


def make_discovery_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
    handoff_note: str = "",
    reasoning_effort: ReasoningEffortSettings | None = None,
    camera_frames_enabled: bool = False,
    response_instructions: str = "",
):
    # The current time is deliberately not inlined into the system text; it
    # comes from the get_current_time tool so the prompt prefix stays
    # byte-identical and eligible for provider prefix caching.
    return _make_llm_node(
        llm,
        tools=tools,
        system_text=DISCOVERY_PROMPT + handoff_note + response_instructions,
        max_tokens=max_tokens,
        parallel_tool_calls=parallel_tool_calls,
        branch_name="discovery",
        reasoning_effort=reasoning_effort,
        camera_frames_enabled=camera_frames_enabled,
    )


def make_background_job_node(
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    *,
    parallel_tool_calls: bool = True,
):
    # ``config`` typing must stay ``Optional[RunnableConfig]``; see _make_llm_node.
    # Background job runs don't expose a reasoning-effort selector (see
    # WotbotState.reasoning_effort docstring), so this always binds without one.
    async def node(state: WotbotState, config: Optional[RunnableConfig] = None):
        system_message = SystemMessage(content=BACKGROUND_JOB_PROMPT + _current_time_block())
        trimmed = _trim_conversation(state["messages"], max_tokens)
        runnable = _prepare_branch_runnable(
            llm,
            tools,
            state=state,
            config=config,
            parallel_tool_calls=parallel_tool_calls,
            branch_name="background_job",
            reasoning_effort=None,
        )
        response = await runnable.ainvoke([system_message, *trimmed])
        return {"messages": [response]}

    return node


def respond_should_continue(state: WotbotState) -> str:
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def make_dispatch_node(targets: dict[str, str], *, finish_node: str):
    """Route a finished action branch into its requested successor branch.

    Reads ``state["next"]`` (set by the route_to handoff tool), clears it, and
    jumps to the mapped target node. When ``next`` is unset/unknown it falls
    through to ``finish_node`` — making the handoff path identical to the
    single-branch flow whenever no handoff was requested. Clearing and jumping
    happen atomically via ``Command`` so a stale field can never re-trigger.
    """

    def dispatch(state: WotbotState) -> Command:
        requested = state.get("next")
        goto = targets.get(requested, finish_node) if requested else finish_node
        if requested and goto == finish_node:
            logger.warning(
                "Agent handoff target unknown requested=%s resolved=%s",
                requested,
                finish_node,
            )
        elif requested:
            logger.info(
                "Agent handoff dispatch requested=%s resolved=%s",
                requested,
                goto,
            )
        else:
            logger.info("Agent handoff dispatch fallthrough resolved=%s", goto)
        return Command(goto=goto, update={"next": None})

    return dispatch
