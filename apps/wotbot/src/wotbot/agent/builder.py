"""LangGraph assembly for the WoTBot agent."""

from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from wotbot.agent.intents import INTENTS
from wotbot.agent.device_interactions import make_device_interaction_summary_node
from wotbot.agent.nodes import (
    WotbotState,
    make_analysis_node,
    make_background_job_node,
    make_control_node,
    make_discovery_node,
    make_dispatch_node,
    make_jobs_node,
    make_respond_node,
    make_router_node,
    make_virtual_things_node,
    respond_should_continue,
)
from wotbot.agent.prompts import HANDOFF_PROMPT, VOICE_RESPONSE_PROMPT
from wotbot.agent.tool_groups import group_local_tools, partition_registry_tools
from wotbot.agent.tools.route_to import make_route_to_tool
from wotbot.core.settings import ReasoningEffortSettings


def _tool_error_message(error: Exception) -> str:
    return f"Tool error: {error}"


def _after_tool_node(next_node: str, *, handoff_node: str | None = None):
    def route(state: WotbotState):
        if handoff_node is not None and state.get("next"):
            return handoff_node
        return next_node

    return route


def _add_action_branch(
    graph: StateGraph,
    *,
    llm_node: str,
    tools_node: str,
    llm_factory,
    llm: ChatOpenAI,
    tools: list[Any],
    max_tokens: int,
    action_branch_end: str,
    handoff_node: str | None,
    parallel_tool_calls: bool,
    handoff_note: str,
    reasoning_effort: ReasoningEffortSettings | None,
    camera_frames_enabled: bool,
    response_instructions: str,
) -> None:
    graph.add_node(
        llm_node,
        llm_factory(
            llm,
            tools,
            max_tokens,
            parallel_tool_calls=parallel_tool_calls,
            handoff_note=handoff_note,
            reasoning_effort=reasoning_effort,
            camera_frames_enabled=camera_frames_enabled,
            response_instructions=response_instructions,
        ),
    )
    graph.add_node(
        tools_node,
        ToolNode(tools, handle_tool_errors=_tool_error_message),
    )
    graph.add_conditional_edges(
        llm_node,
        tools_condition,
        {
            "tools": tools_node,
            END: action_branch_end,
        },
    )
    after_tool_targets = {llm_node: llm_node, END: END}
    if handoff_node is not None:
        after_tool_targets[handoff_node] = handoff_node
    graph.add_conditional_edges(
        tools_node,
        _after_tool_node(llm_node, handoff_node=handoff_node),
        after_tool_targets,
    )


def build_graph(
    llm: ChatOpenAI,
    registry_tools: list[Any],
    local_tools: list[Any],
    max_tokens: int,
    checkpointer=None,
    parallel_tool_calls: bool = True,
    camera_frames_enabled: bool = False,
    handoff_enabled: bool = False,
    reasoning_effort: ReasoningEffortSettings | None = None,
    voice_mode: bool = False,
):
    """Build and compile the wotbot agent StateGraph."""
    registry_tool_groups = partition_registry_tools(registry_tools)
    local_tool_groups = group_local_tools(local_tools)
    response_instructions = VOICE_RESPONSE_PROMPT if voice_mode else ""

    web_interface_tools = (
        [local_tool_groups.create_web_interface] if local_tool_groups.create_web_interface else []
    )
    job_runtime_tools = [
        tool
        for tool in (
            local_tool_groups.ask_job_user,
            local_tool_groups.submit_job_record,
        )
        if tool
    ]
    respond_tools = [local_tool_groups.get_current_time, *job_runtime_tools]
    control_tools = (
        registry_tool_groups.discovery_and_inspect
        + registry_tool_groups.runtime
        + web_interface_tools
        + job_runtime_tools
    )
    analysis_tools = (
        registry_tool_groups.discovery_and_inspect
        + registry_tool_groups.runtime_read
        + [local_tool_groups.run_code, local_tool_groups.get_current_time]
        + web_interface_tools
        + job_runtime_tools
    )
    jobs_tools = (
        registry_tool_groups.discovery_and_inspect
        + registry_tool_groups.runtime_read
        + [local_tool_groups.run_code, local_tool_groups.get_current_time]
        + job_runtime_tools
        + local_tool_groups.job_tools
    )
    virtual_things_tools = (
        registry_tool_groups.discovery_and_inspect
        + registry_tool_groups.virtual_authoring_runtime
        + [local_tool_groups.get_current_time]
        + local_tool_groups.virtual_thing_tools
    )
    discovery_tools = (
        registry_tool_groups.discovery
        + registry_tool_groups.discovery_authoring
        + registry_tool_groups.runtime
        + local_tool_groups.external_discovery_tools
        + job_runtime_tools
    )

    # When handoff is enabled, give the action branches the route_to tool so they
    # can continue the turn in another branch, and a system-prompt note telling
    # them how. Off by default: the tool lists, prompts, and edges below stay
    # exactly as the single-branch graph.
    handoff_note = ""
    if handoff_enabled:
        route_to = make_route_to_tool()
        control_tools = control_tools + [route_to]
        analysis_tools = analysis_tools + [route_to]
        jobs_tools = jobs_tools + [route_to]
        virtual_things_tools = virtual_things_tools + [route_to]
        discovery_tools = discovery_tools + [route_to]
        handoff_note = HANDOFF_PROMPT

    graph = StateGraph(WotbotState)

    graph.add_node("router", make_router_node(llm, max_tokens))
    graph.add_node(
        "respond",
        make_respond_node(
            llm,
            respond_tools,
            max_tokens,
            parallel_tool_calls=parallel_tool_calls,
            reasoning_effort=reasoning_effort,
            camera_frames_enabled=camera_frames_enabled,
            response_instructions=response_instructions,
        ),
    )
    graph.add_node(
        "respond_tools",
        ToolNode(respond_tools, handle_tool_errors=_tool_error_message),
    )
    graph.add_node("device_summary", make_device_interaction_summary_node())

    # Action branches end at "dispatch" (which either continues into another
    # branch or falls through to device_summary) when handoff is enabled,
    # otherwise straight at "device_summary" as in the single-branch graph.
    action_branch_end = "device_summary"
    handoff_node = None
    if handoff_enabled:
        action_branch_end = "dispatch"
        handoff_node = "dispatch"
        graph.add_node(
            "dispatch",
            make_dispatch_node(
                {
                    "control": "control_llm",
                    "analysis": "analysis_llm",
                    "jobs": "jobs_llm",
                    "virtual_things": "virtual_things_llm",
                    "discovery": "discovery_llm",
                },
                finish_node="device_summary",
            ),
        )

    graph.add_edge(START, "router")

    for branch in (
        (
            "jobs_llm",
            "jobs_tools",
            make_jobs_node,
            jobs_tools,
        ),
        (
            "virtual_things_llm",
            "virtual_things_tools",
            make_virtual_things_node,
            virtual_things_tools,
        ),
        (
            "discovery_llm",
            "discovery_tools",
            make_discovery_node,
            discovery_tools,
        ),
        (
            "control_llm",
            "control_tools",
            make_control_node,
            control_tools,
        ),
        (
            "analysis_llm",
            "analysis_tools",
            make_analysis_node,
            analysis_tools,
        ),
    ):
        llm_node, tools_node, llm_factory, tools = branch
        _add_action_branch(
            graph,
            llm_node=llm_node,
            tools_node=tools_node,
            llm_factory=llm_factory,
            llm=llm,
            tools=tools,
            max_tokens=max_tokens,
            action_branch_end=action_branch_end,
            handoff_node=handoff_node,
            parallel_tool_calls=parallel_tool_calls,
            handoff_note=handoff_note,
            reasoning_effort=reasoning_effort,
            camera_frames_enabled=camera_frames_enabled,
            response_instructions=response_instructions,
        )

    graph.add_conditional_edges(
        "router",
        lambda state: state.get("intent", "chat"),
        {intent.id: intent.entry for intent in INTENTS.values()},
    )

    graph.add_conditional_edges(
        "respond",
        respond_should_continue,
        {
            "tools": "respond_tools",
            END: "device_summary",
        },
    )
    graph.add_conditional_edges(
        "respond_tools",
        _after_tool_node("respond"),
        {
            "respond": "respond",
            END: END,
        },
    )

    graph.add_edge("device_summary", END)

    return graph.compile(checkpointer=checkpointer)


def build_background_job_graph(
    llm: ChatOpenAI,
    registry_tools: list[Any],
    local_tools: list[Any],
    max_tokens: int,
    checkpointer=None,
    parallel_tool_calls: bool = True,
):
    """Build and compile the compact graph used by background prompt jobs."""
    registry_tool_groups = partition_registry_tools(registry_tools)
    local_tool_groups = group_local_tools(local_tools)
    job_tools = (
        registry_tool_groups.discovery_and_inspect
        + registry_tool_groups.runtime
        + [local_tool_groups.run_code, local_tool_groups.get_current_time]
        + [
            tool
            for tool in (
                local_tool_groups.ask_job_user,
                local_tool_groups.submit_job_record,
            )
            if tool
        ]
    )

    graph = StateGraph(WotbotState)
    graph.add_node(
        "job_llm",
        make_background_job_node(
            llm,
            job_tools,
            max_tokens,
            parallel_tool_calls=parallel_tool_calls,
        ),
    )
    graph.add_node(
        "job_tools",
        ToolNode(job_tools, handle_tool_errors=_tool_error_message),
    )
    graph.add_edge(START, "job_llm")
    graph.add_conditional_edges(
        "job_llm",
        tools_condition,
        {
            "tools": "job_tools",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "job_tools",
        _after_tool_node("job_llm"),
        {
            "job_llm": "job_llm",
            END: END,
        },
    )
    return graph.compile(checkpointer=checkpointer)
