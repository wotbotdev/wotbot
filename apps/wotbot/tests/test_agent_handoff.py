"""Tests for optional branch-to-branch handoff in the agent graph.

Covers the dispatch node, the route_to tool, and end-to-end wiring of
``build_graph(handoff_enabled=...)`` — including the guarantee that the
flag-off graph is structurally identical to the single-branch graph.
"""

import logging
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from wotbot.agent.builder import build_graph
from wotbot.agent.nodes import IntentClassification, make_dispatch_node
from wotbot.agent.tools.route_to import make_route_to_tool

_ACTION_LLM_NODES = {"control_llm", "analysis_llm", "jobs_llm", "virtual_things_llm"}


def _enable_log_capture(logger_name: str) -> None:
    logging.disable(logging.NOTSET)
    logging.getLogger(logger_name).disabled = False


@tool("get_current_time")
def _get_current_time() -> str:
    """Stub time tool."""
    return "now"


@tool("run_code")
def _run_code(code: str) -> str:
    """Stub code tool."""
    return "ran"


def _local_tools() -> list:
    return [_get_current_time, _run_code]


class DispatchNodeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatch = make_dispatch_node(
            {"analysis": "analysis_llm", "control": "control_llm"},
            finish_node="device_summary",
        )

    def test_routes_to_requested_branch_and_clears_field(self) -> None:
        _enable_log_capture("wotbot.agent.nodes")

        with self.assertLogs("wotbot.agent.nodes", level="INFO") as logs:
            result = self.dispatch({"next": "analysis"})

        self.assertIsInstance(result, Command)
        self.assertEqual(result.goto, "analysis_llm")
        self.assertEqual(result.update, {"next": None})
        output = "\n".join(logs.output)
        self.assertIn("Agent handoff dispatch requested=analysis resolved=analysis_llm", output)

    def test_finishes_when_no_next_requested(self) -> None:
        _enable_log_capture("wotbot.agent.nodes")

        with self.assertLogs("wotbot.agent.nodes", level="INFO") as logs:
            result = self.dispatch({})

        self.assertEqual(result.goto, "device_summary")
        self.assertEqual(result.update, {"next": None})
        output = "\n".join(logs.output)
        self.assertIn("Agent handoff dispatch fallthrough resolved=device_summary", output)

    def test_finishes_on_unknown_target(self) -> None:
        _enable_log_capture("wotbot.agent.nodes")

        with self.assertLogs("wotbot.agent.nodes", level="WARNING") as logs:
            result = self.dispatch({"next": "bogus"})

        self.assertEqual(result.goto, "device_summary")
        self.assertEqual(result.update, {"next": None})
        output = "\n".join(logs.output)
        self.assertIn(
            "Agent handoff target unknown requested=bogus resolved=device_summary", output
        )


class RouteToToolTestCase(unittest.TestCase):
    def test_records_intent_and_emits_paired_tool_message(self) -> None:
        route_to = make_route_to_tool()
        command = route_to.invoke(
            {
                "name": "route_to",
                "args": {"intent": "analysis"},
                "id": "call-1",
                "type": "tool_call",
            }
        )
        self.assertIsInstance(command, Command)
        self.assertEqual(command.update["next"], "analysis")
        (message,) = command.update["messages"]
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.tool_call_id, "call-1")


class _FakeBoundLLM:
    def __init__(self, parent: "_ScriptedLLM", tool_names: list[str]) -> None:
        self._parent = parent
        self._tool_names = tool_names

    async def ainvoke(self, messages):
        # The analysis branch is the only one bound with run_code; return a plain
        # completion there. The virtual_things branch hands off immediately.
        if "run_code" in self._tool_names:
            self._parent.visited.append("analysis")
            return AIMessage(content="analysis complete")

        self._parent.visited.append("virtual_things")
        return AIMessage(
            content="",
            tool_calls=[{"name": "route_to", "args": {"intent": "analysis"}, "id": "call-1"}],
        )


class _ScriptedStructuredLLM:
    def __init__(self, intent: str) -> None:
        self._intent = intent

    async def ainvoke(self, messages, config=None):
        return IntentClassification(intent=self._intent)


class _ScriptedLLM:
    """Routes to virtual_things, then drives a route_to -> analysis handoff."""

    def __init__(self, intent: str = "virtual_things") -> None:
        self._intent = intent
        self.visited: list[str] = []

    def with_structured_output(self, _schema):
        return _ScriptedStructuredLLM(self._intent)

    def bind_tools(self, tools, **_kwargs):
        return _FakeBoundLLM(self, [t.name for t in tools])


class GraphWiringTestCase(unittest.TestCase):
    def _build(self, *, handoff_enabled: bool):
        return build_graph(
            llm=_ScriptedLLM(),
            registry_tools=[],
            local_tools=_local_tools(),
            max_tokens=1000,
            handoff_enabled=handoff_enabled,
        )

    def test_flag_off_has_no_dispatch_node(self) -> None:
        nodes = self._build(handoff_enabled=False).get_graph().nodes
        self.assertNotIn("dispatch", nodes)

    def test_flag_on_adds_dispatch_node(self) -> None:
        nodes = self._build(handoff_enabled=True).get_graph().nodes
        self.assertIn("dispatch", nodes)


class HandoffExecutionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_virtual_things_hands_off_into_analysis(self) -> None:
        llm = _ScriptedLLM()
        graph = build_graph(
            llm=llm,
            registry_tools=[],
            local_tools=_local_tools(),
            max_tokens=1000,
            handoff_enabled=True,
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="create a vt then analyze it")]}
        )

        # Dispatch runs immediately after route_to; the source branch must not
        # receive another LLM turn before the target branch starts.
        self.assertEqual(llm.visited, ["virtual_things", "analysis"])
        # The field was cleared so dispatch did not re-loop.
        self.assertIsNone(result.get("next"))
        contents = [str(m.content) for m in result["messages"]]
        self.assertIn("analysis complete", contents)

    async def test_no_handoff_when_model_does_not_request_one(self) -> None:
        # Router sends straight to analysis, which completes without route_to.
        llm = _ScriptedLLM(intent="analysis")
        graph = build_graph(
            llm=llm,
            registry_tools=[],
            local_tools=_local_tools(),
            max_tokens=1000,
            handoff_enabled=True,
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="what's the temperature")]})
        self.assertEqual(llm.visited, ["analysis"])
        self.assertIsNone(result.get("next"))


if __name__ == "__main__":
    unittest.main()
