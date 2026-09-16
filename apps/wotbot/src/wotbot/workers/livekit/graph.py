"""Adapt the LangGraph agent graph for the LiveKit voice session."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from wotbot.agent import build_graph
from wotbot.agent.tools import LOCAL_TOOLS, REGISTRY_TOOLS
from wotbot.agent.voice import voice_stream_text_from_event
from wotbot.core.llm import make_llm
from wotbot.core.settings import Settings


def _latest_user_turn_state(state: Any) -> Any:
    """Reduce adapter input to just the newest user message.

    The LiveKit ``LLMAdapter`` replays the entire ``chat_ctx`` as graph input on
    every turn, but the graph already persists history through its checkpointer.
    Feeding both duplicates every assistant message in state (the replayed
    copies carry LiveKit ids that don't match the checkpointed ones, so
    ``add_messages`` can't dedupe them), which eventually makes the model echo
    its own answers. Keep only the latest human turn and let the checkpointer
    own history.
    """
    if not isinstance(state, dict):
        return state
    messages = state.get("messages")
    if not isinstance(messages, list):
        return state
    last_human = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return state
    return {**state, "messages": [last_human]}


class VoiceSafeGraphStream:
    """Filter LangGraph message streams to voice-safe assistant text chunks."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    def astream(self, *args: Any, **kwargs: Any):
        if args:
            args = (_latest_user_turn_state(args[0]), *args[1:])
        events = self._graph.astream(*args, **kwargs)

        async def filtered_events():
            async for event in events:
                if voice_stream_text_from_event(event):
                    yield event

        return filtered_events()


def compile_graph(settings: Settings, checkpointer: Any):
    """Build the agent graph for a voice session, bound to the recursion limit."""
    llm = make_llm(settings)
    graph = build_graph(
        llm=llm,
        registry_tools=REGISTRY_TOOLS,
        local_tools=LOCAL_TOOLS,
        max_tokens=settings.max_context_tokens,
        checkpointer=checkpointer,
        parallel_tool_calls=settings.parallel_tool_calls,
        camera_frames_enabled=settings.openai_model_supports_vision,
        voice_mode=True,
        agent_system_prompt_extra=settings.agent_system_prompt_extra,
    )
    return graph.with_config(recursion_limit=settings.recursion_limit)
