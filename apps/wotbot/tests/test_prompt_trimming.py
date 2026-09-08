import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wotbot.agent.device_interactions import DEVICE_INTERACTION_SUMMARY_TYPE
from wotbot.agent.nodes import (
    _make_router_messages,
    _trim_conversation,
)


def _tool_call(tool_id: str) -> dict:
    return {
        "id": tool_id,
        "name": "run_code",
        "args": {"code": "print('hello')"},
        "type": "tool_call",
    }


class PromptTrimmingTestCase(unittest.TestCase):
    def test_file_download_locators_stay_in_state_but_out_of_model_context(self) -> None:
        file_summary = {
            "ref": "file_1",
            "kind": "file",
            "filename": "forecast.csv",
            "mime_type": "text/csv",
            "size_bytes": 1267,
            "expires_at": "2026-09-15T14:00:06+00:00",
        }
        artifact = {
            **file_summary,
            "id": "file-internal.csv",
            "uri": "wotbot://artifacts/file-internal.csv",
            "content_uri": "wotbot://artifacts/file-internal.csv/content",
        }
        chart = {"ref": "chart_1", "kind": "plotly", "filename": "chart.json"}
        # File-only executions may have no wot_calls to strip.
        result = {
            "ok": True,
            "stdout": "Saved 10 forecast rows. Source: https://example.com/weather",
            "artifacts": [artifact, chart],
        }
        original = ToolMessage(
            content=json.dumps(result), name="run_code", tool_call_id="call-1", id="result-1"
        )
        messages = [
            HumanMessage(content="Save the forecast as a CSV"),
            AIMessage(content="", tool_calls=[_tool_call("call-1")]),
            original,
        ]

        trimmed = _trim_conversation(messages, max_tokens=10_000)

        model_result = trimmed[-1]
        self.assertEqual(
            json.loads(model_result.content), {**result, "artifacts": [file_summary, chart]}
        )
        self.assertEqual(model_result.id, original.id)
        self.assertEqual(model_result.tool_call_id, original.tool_call_id)
        self.assertEqual(model_result.name, "run_code")
        self.assertIs(messages[-1], original)
        self.assertEqual(json.loads(original.content), result)

    def test_trim_conversation_preserves_tool_context(self) -> None:
        """Tool calls and results from all turns should survive trimming."""
        messages = [
            HumanMessage(content="Show the last 24h"),
            AIMessage(content="", tool_calls=[_tool_call("call-old")]),
            ToolMessage(content='{"rows":[1,2,3]}', tool_call_id="call-old"),
            AIMessage(content="Here is the 24h summary."),
            HumanMessage(content="Now compare with the last 48h"),
            AIMessage(content="", tool_calls=[_tool_call("call-current")]),
            ToolMessage(content='{"rows":[4,5,6]}', tool_call_id="call-current"),
        ]

        trimmed = _trim_conversation(messages, max_tokens=10_000)

        types = [type(m).__name__ for m in trimmed]
        self.assertEqual(
            types,
            [
                "HumanMessage",
                "AIMessage",
                "ToolMessage",
                "AIMessage",
                "HumanMessage",
                "AIMessage",
                "ToolMessage",
            ],
        )

    def test_router_messages_strip_tool_context(self) -> None:
        """The router should only see conversational messages."""
        messages = [
            HumanMessage(content="Show the last 24h"),
            AIMessage(content="", tool_calls=[_tool_call("call-old")]),
            ToolMessage(content='{"rows":[1,2,3]}', tool_call_id="call-old"),
            AIMessage(content="Energy use over the last 24h is 10 kWh."),
            HumanMessage(content="and the last 72h?"),
        ]

        tail = _make_router_messages(messages, max_tokens=10_000)

        # Router strips tool messages and AI messages with tool_calls
        self.assertTrue(all(not isinstance(m, ToolMessage) for m in tail))
        self.assertTrue(
            all(not (isinstance(m, AIMessage) and m.tool_calls) for m in tail),
        )
        # Should keep the conversational messages
        contents = [m.content for m in tail]
        self.assertIn("and the last 72h?", contents)

    def test_huge_wot_calls_payload_does_not_evict_the_human_turn(self) -> None:
        """A multi-MB wot_calls blob is stripped before counting, so the real
        conversation survives a tight token budget (regression for the thread
        that reset to a fresh greeting after a large analysis run)."""
        bulky_wot_calls = [
            {
                "type": "invoke_action",
                "thing_id": "urn:predict",
                "name": "predict",
                "input": [{"time": str(i), "value": i} for i in range(8000)],
            }
        ]
        tool_content = json.dumps({"stdout": "Fetched 6529 readings", "wot_calls": bulky_wot_calls})

        messages = [
            HumanMessage(content="Run all NILM models on the smart meter"),
            AIMessage(content="", tool_calls=[_tool_call("call-1")]),
            ToolMessage(content=tool_content, tool_call_id="call-1"),
        ]

        trimmed = _trim_conversation(messages, max_tokens=2_000)

        contents = [m.content for m in trimmed]
        self.assertIn("Run all NILM models on the smart meter", contents)
        tool_messages = [m for m in trimmed if isinstance(m, ToolMessage)]
        self.assertTrue(tool_messages, "the run_code tool result should survive trimming")
        self.assertNotIn("wot_calls", tool_messages[0].content)

    def test_trim_conversation_recovers_human_message_evicted_by_a_bulky_tool_turn(
        self,
    ) -> None:
        """A token-tight budget can otherwise trim a turn down to nothing but
        tool-call/tool-response content. Some model chat templates (Qwen3.5's,
        served via vLLM) reject that outright with "No user query found in
        messages." — the trimmed conversation must always keep the HumanMessage
        that started the current turn, if one exists at all."""
        tool_content = json.dumps({"rows": list(range(2000))})
        messages = [
            HumanMessage(content="Summarize the last week of readings"),
            AIMessage(content="", tool_calls=[_tool_call("call-1")]),
            ToolMessage(content=tool_content, tool_call_id="call-1"),
            AIMessage(content="", tool_calls=[_tool_call("call-2")]),
            ToolMessage(content=tool_content, tool_call_id="call-2"),
        ]

        trimmed = _trim_conversation(messages, max_tokens=200)

        self.assertTrue(any(isinstance(m, HumanMessage) for m in trimmed))
        self.assertEqual(trimmed[0].content, "Summarize the last week of readings")

    def test_trim_conversation_leaves_tool_only_history_alone_without_a_human_message(
        self,
    ) -> None:
        """Nothing to recover: a conversation with no HumanMessage at all (e.g.
        a background job run) is returned unchanged rather than fabricating
        one."""
        messages = [
            AIMessage(content="", tool_calls=[_tool_call("call-1")]),
            ToolMessage(content='{"ok": true}', tool_call_id="call-1"),
        ]

        trimmed = _trim_conversation(messages, max_tokens=10_000)

        self.assertFalse(any(isinstance(m, HumanMessage) for m in trimmed))

    def test_ui_device_summary_messages_are_not_sent_to_llm_prompts(self) -> None:
        summary = AIMessage(
            content=json.dumps(
                {
                    "type": DEVICE_INTERACTION_SUMMARY_TYPE,
                    "interactions": [
                        {
                            "type": "read_property",
                            "thingId": "urn:meter",
                            "affordanceName": "power",
                            "ok": True,
                        }
                    ],
                }
            )
        )
        messages = [
            HumanMessage(content="Show power"),
            AIMessage(content="Power is 10 kW."),
            summary,
            HumanMessage(content="and yesterday?"),
        ]

        trimmed = _trim_conversation(messages, max_tokens=10_000)
        routed = _make_router_messages(messages, max_tokens=10_000)

        self.assertNotIn(summary, trimmed)
        self.assertNotIn(summary, routed)


def _assert_no_dangling_tool_calls(case: unittest.TestCase, messages: list) -> None:
    """A trimmed sequence must never send an AI tool_call with no matching
    ToolMessage right after it, or a ToolMessage with nothing before it. This
    used to require a post-hoc repair pass (``_sanitize_message_sequence``
    run *after* cutting); trimming by whole units means it can't happen in
    the first place, so this checks the invariant holds regardless of how
    tight the budget is."""
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            expected_ids = {tc["id"] for tc in message.tool_calls}
            following_ids = {
                m.tool_call_id for m in messages[index + 1 :] if isinstance(m, ToolMessage)
            }
            case.assertTrue(
                expected_ids.issubset(following_ids),
                f"AI tool_calls {expected_ids} missing matching ToolMessage in {following_ids}",
            )
        if isinstance(message, ToolMessage):
            preceding = messages[index - 1] if index > 0 else None
            case.assertTrue(
                isinstance(preceding, (AIMessage, ToolMessage)),
                "ToolMessage with no preceding AI tool_calls message",
            )


class TrimStructuralInvariantsTestCase(unittest.TestCase):
    def test_tight_budget_never_leaves_a_dangling_tool_call(self) -> None:
        """Regression for the class of bug ``_ensure_human_message`` and
        ``_sanitize_message_sequence`` used to patch reactively: even under a
        budget too tight to fit anything comfortably, the output must stay a
        structurally valid sequence."""
        messages = [
            HumanMessage(content="Summarize the last week of readings"),
            AIMessage(content="", tool_calls=[_tool_call("call-1")]),
            ToolMessage(content=json.dumps({"rows": list(range(2000))}), tool_call_id="call-1"),
            AIMessage(content="", tool_calls=[_tool_call("call-2")]),
            ToolMessage(content=json.dumps({"rows": list(range(2000))}), tool_call_id="call-2"),
        ]

        for budget in (1, 10, 50, 200, 1_000, 10_000):
            with self.subTest(budget=budget):
                trimmed = _trim_conversation(messages, max_tokens=budget)
                _assert_no_dangling_tool_calls(self, trimmed)
                self.assertTrue(any(isinstance(m, HumanMessage) for m in trimmed))

    def test_partial_parallel_tool_results_never_leave_a_dangling_call_id(self) -> None:
        """An AI message that made 2 parallel tool calls but only has 1
        ToolMessage in state (e.g. a checkpoint saved mid-turn) must come out
        patched to reference only the tool_call it actually has a result for,
        under any budget -- not just when the budget happens to be generous
        enough for sanitize to run on the full, untrimmed list."""
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "wot_get_action", "args": {}, "id": "call_a"},
                {"name": "wot_get_action", "args": {}, "id": "call_b"},
            ],
        )
        messages = [
            HumanMessage(content="Inspect both"),
            ai,
            ToolMessage(content=json.dumps({"schema": list(range(2000))}), tool_call_id="call_a"),
        ]

        for budget in (1, 50, 500, 10_000):
            with self.subTest(budget=budget):
                trimmed = _trim_conversation(messages, max_tokens=budget)
                _assert_no_dangling_tool_calls(self, trimmed)


if __name__ == "__main__":
    unittest.main()
