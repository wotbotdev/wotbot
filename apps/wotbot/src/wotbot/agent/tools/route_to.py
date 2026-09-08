"""Handoff tool letting an action branch continue into another branch.

Only bound into the action branches when ``agent_handoff_enabled`` is set. The
tool records the chosen next branch in ``state["next"]``; the shared ``dispatch``
node (see ``agent.nodes.make_dispatch_node``) reads that field, clears it, and
jumps to the target branch's ``*_llm`` node. Routing therefore lives in one
place, and the tool only expresses the agent's choice.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId
from langgraph.types import Command

from wotbot.agent.tools.contracts import tool

# Intents an action branch may hand off to. Mirrors the dispatch target map in
# ``builder.build_graph``. ``respond``/chat is intentionally excluded.
HandoffIntent = Literal["control", "analysis", "jobs", "virtual_things"]


def make_route_to_tool() -> Any:
    """Build the ``route_to`` handoff tool."""

    @tool
    def route_to(
        intent: HandoffIntent,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Continue this turn in another branch once the current task is done.

        Call this only when the current task is complete and the user's request
        clearly needs follow-up work handled by a different area:

        - ``control``: perform a Thing action or build a control panel.
        - ``analysis``: read, explore, visualise, or compute over data.
        - ``jobs``: create, inspect, run, or debug an automation job.
        - ``virtual_things``: create, update, or test a computed/virtual Thing.

        After calling this, stop — the handoff happens automatically.
        """
        return Command(
            update={
                "next": intent,
                "messages": [ToolMessage(f"Continuing in {intent}.", tool_call_id=tool_call_id)],
            }
        )

    return route_to
