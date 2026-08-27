import inspect
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wotbot.agent.camera_context import CameraFrameAttachment
from wotbot.agent.nodes import (
    IntentClassification,
    _latest_run_code_source,
    _make_llm_node,
    _make_router_messages,
    _prior_analysis_block,
    _resolve_reasoning_effort,
    _sanitize_message_sequence,
    _strip_wot_calls,
    make_analysis_node,
    make_control_node,
    make_jobs_node,
    make_respond_node,
    make_router_node,
    make_virtual_things_node,
)
from wotbot.core.settings import ReasoningEffortSettings, ReasoningEffortStyle


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _enable_log_capture(logger_name: str) -> None:
    logging.disable(logging.NOTSET)
    logging.getLogger(logger_name).disabled = False


class _FakeBoundLLM:
    def __init__(self, parent: "_FakeLLM") -> None:
        self._parent = parent

    async def ainvoke(self, messages):
        self._parent.invocations.append(("bound", messages))
        return AIMessage(content="ok")


class _FakeLLM:
    def __init__(self) -> None:
        self.bound_tools: list[list[str]] = []
        self.bound_kwargs: list[dict] = []
        self.plain_bind_kwargs: list[dict] = []
        self.invocations: list[tuple[str, object]] = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append([tool.name for tool in tools])
        self.bound_kwargs.append(kwargs)
        return _FakeBoundLLM(self)

    def bind(self, **kwargs):
        self.plain_bind_kwargs.append(kwargs)
        return _FakeBoundLLM(self)

    async def ainvoke(self, messages):
        self.invocations.append(("plain", messages))
        return AIMessage(content="ok")


class _FakeStructuredLLM:
    def __init__(self) -> None:
        self.configs: list[object] = []

    async def ainvoke(self, _messages, config=None):
        self.configs.append(config)
        return IntentClassification(intent="analysis")


class _FakeRouterLLM:
    def __init__(self) -> None:
        self.structured = _FakeStructuredLLM()

    def with_structured_output(self, _schema):
        return self.structured


class NodeMessageSanitizationTestCase(unittest.TestCase):
    def test_router_schema_accepts_jobs_intent(self) -> None:
        self.assertEqual(IntentClassification(intent="jobs").intent, "jobs")

    def test_router_schema_accepts_virtual_things_intent(self) -> None:
        self.assertEqual(
            IntentClassification(intent="virtual_things").intent,
            "virtual_things",
        )

    def test_sanitize_message_sequence_drops_orphan_tool_messages(self) -> None:
        messages = [
            HumanMessage(content="First request"),
            AIMessage(
                content="", tool_calls=[{"name": "things_search", "args": {}, "id": "call_1"}]
            ),
            ToolMessage(content='[{"id":"thing-1"}]', tool_call_id="call_1"),
            AIMessage(content="Found the thing."),
            ToolMessage(content='{"unexpected":true}', tool_call_id="orphan"),
            HumanMessage(content="Second request"),
            AIMessage(
                content="", tool_calls=[{"name": "wot_get_action", "args": {}, "id": "call_2"}]
            ),
        ]

        sanitized = _sanitize_message_sequence(messages)

        self.assertEqual(
            sanitized,
            [
                messages[0],
                messages[1],
                messages[2],
                messages[3],
                messages[5],
            ],
        )

    def test_make_router_messages_filters_out_tool_turns(self) -> None:
        messages = [
            HumanMessage(content="Show me line 5 throughput"),
            AIMessage(
                content="", tool_calls=[{"name": "things_search", "args": {}, "id": "call_1"}]
            ),
            ToolMessage(content='[{"id":"line-counter-05"}]', tool_call_id="call_1"),
            AIMessage(content="I found the production counter."),
            HumanMessage(content="Now break it down with all matching analysis services."),
        ]

        router_messages = _make_router_messages(messages, max_tokens=4000)

        self.assertEqual(
            router_messages,
            [
                messages[0],
                messages[3],
                messages[4],
            ],
        )

    def test_sanitize_patches_ai_with_partial_tool_results(self) -> None:
        """AI made 2 tool calls but only 1 ToolMessage exists (trimmed parallel call)."""
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "wot_get_action", "args": {}, "id": "call_a"},
                {"name": "wot_get_action", "args": {}, "id": "call_b"},
            ],
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "wot_get_action", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "wot_get_action", "arguments": "{}"},
                    },
                ]
            },
        )
        messages = [
            HumanMessage(content="Inspect both"),
            ai,
            ToolMessage(content='{"schema": "ok"}', tool_call_id="call_a"),
        ]

        sanitized = _sanitize_message_sequence(messages)

        self.assertEqual(len(sanitized), 3)
        # AI message should be patched to only reference call_a
        self.assertEqual(len(sanitized[1].tool_calls), 1)
        self.assertEqual(sanitized[1].tool_calls[0]["id"], "call_a")
        self.assertEqual(len(sanitized[1].additional_kwargs["tool_calls"]), 1)

    def test_sanitize_does_not_mutate_ai_messages_when_patching(self) -> None:
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "wot_get_action", "args": {}, "id": "call_a"},
                {"name": "wot_get_action", "args": {}, "id": "call_b"},
            ],
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "wot_get_action", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "wot_get_action", "arguments": "{}"},
                    },
                ]
            },
        )

        _sanitize_message_sequence(
            [
                HumanMessage(content="Inspect both"),
                ai,
                ToolMessage(content='{"schema": "a"}', tool_call_id="call_a"),
            ]
        )

        self.assertEqual([call["id"] for call in ai.tool_calls], ["call_a", "call_b"])
        self.assertEqual(len(ai.additional_kwargs["tool_calls"]), 2)

    def test_sanitize_keeps_all_parallel_results(self) -> None:
        """AI made 2 tool calls and both ToolMessages exist — keep everything."""
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
            ToolMessage(content='{"schema": "a"}', tool_call_id="call_a"),
            ToolMessage(content='{"schema": "b"}', tool_call_id="call_b"),
            AIMessage(content="Both inspected."),
        ]

        sanitized = _sanitize_message_sequence(messages)

        self.assertEqual(len(sanitized), 5)
        self.assertEqual(len(sanitized[1].tool_calls), 2)


class StripWotCallsTestCase(unittest.TestCase):
    def test_removes_wot_calls_from_json_tool_message(self) -> None:
        import json

        original = json.dumps(
            {
                "stdout": "hello",
                "artifacts": [{"ref": "chart_1", "kind": "plotly", "filename": "abc.json"}],
                "wot_calls": [{"type": "invoke_action", "thing_id": "urn:1", "name": "get_power"}],
            }
        )
        msg = ToolMessage(content=original, tool_call_id="call_1")
        result = _strip_wot_calls(msg)

        parsed = json.loads(result.content)
        self.assertIn("stdout", parsed)
        self.assertIn("artifacts", parsed)
        self.assertNotIn("wot_calls", parsed)

    def test_preserves_non_json_content(self) -> None:
        msg = ToolMessage(content="plain text result", tool_call_id="call_1")
        result = _strip_wot_calls(msg)
        self.assertEqual(result.content, "plain text result")

    def test_preserves_json_without_wot_calls(self) -> None:
        import json

        original = json.dumps({"stdout": "ok", "artifacts": []})
        msg = ToolMessage(content=original, tool_call_id="call_1")
        result = _strip_wot_calls(msg)
        self.assertEqual(result.content, original)

    def test_passes_through_non_tool_messages(self) -> None:
        msg = HumanMessage(content="hello")
        result = _strip_wot_calls(msg)
        self.assertIs(result, msg)

    def test_does_not_mutate_original_message(self) -> None:
        import json

        original = json.dumps({"stdout": "x", "wot_calls": [{"type": "read"}]})
        msg = ToolMessage(content=original, tool_call_id="call_1")
        _strip_wot_calls(msg)
        self.assertIn("wot_calls", msg.content)


class DynamicToolBindingTestCase(unittest.IsolatedAsyncioTestCase):
    def test_llm_node_config_annotation_allows_langgraph_injection(self) -> None:
        node = _make_llm_node(
            _FakeLLM(),
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
        )

        signature = inspect.signature(node)

        self.assertEqual(signature.parameters["config"].annotation, "Optional[RunnableConfig]")

    async def test_llm_node_attaches_camera_frame_when_main_model_supports_it(self) -> None:
        llm = _FakeLLM()
        prepared = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": "frame"}}])]
        node = _make_llm_node(
            llm,
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
            camera_frames_enabled=True,
        )

        attach = AsyncMock(
            return_value=CameraFrameAttachment(
                messages=prepared,
                attached=True,
                captured_at="now",
            )
        )
        with patch("wotbot.agent.nodes.attach_latest_camera_frame", attach):
            await node(
                {"messages": [HumanMessage(content="what is this?")]},
                {"configurable": {"thread_id": "thread-1"}},
            )

        self.assertEqual(llm.bound_tools, [["get_current_time"]])
        self.assertEqual(llm.invocations[0], ("bound", prepared))
        attach.assert_awaited_once()
        self.assertEqual(attach.await_args.kwargs["thread_id"], "thread-1")

    async def test_llm_node_logs_branch_metadata_without_message_content(self) -> None:
        _enable_log_capture("wotbot.agent.nodes")

        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
            branch_name="respond",
        )

        with self.assertLogs("wotbot.agent.nodes", level="DEBUG") as logs:
            await node(
                {"messages": [HumanMessage(content="secret user text")]},
                {"configurable": {"thread_id": "thread-1"}},
            )

        output = "\n".join(logs.output)
        self.assertIn("Agent branch entered branch=respond", output)
        self.assertIn("thread_id=thread-1", output)
        self.assertIn("tool_count=1", output)
        self.assertIn("get_current_time", output)
        self.assertIn("parallel_tool_calls=True", output)
        self.assertNotIn("secret user text", output)

    async def test_llm_node_skips_camera_lookup_when_capability_is_disabled(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
        )

        attach = AsyncMock()
        with patch("wotbot.agent.nodes.attach_latest_camera_frame", attach):
            await node(
                {"messages": [HumanMessage(content="list my devices")]},
                {"configurable": {"thread_id": "thread-1"}},
            )

        attach.assert_not_awaited()
        self.assertEqual(llm.bound_tools, [["get_current_time"]])
        self.assertEqual(llm.invocations[0][0], "bound")


def _effort_settings(
    levels: frozenset[str], style: ReasoningEffortStyle = "openai"
) -> ReasoningEffortSettings:
    return ReasoningEffortSettings(enabled=True, levels=tuple(levels), default=None, style=style)


class ReasoningEffortBindingTestCase(unittest.IsolatedAsyncioTestCase):
    def test_resolve_reasoning_effort_requires_allow_listed_value(self) -> None:
        allowed = _effort_settings(frozenset({"low", "medium", "high"}))

        self.assertEqual(_resolve_reasoning_effort({"reasoning_effort": "high"}, allowed), "high")
        self.assertIsNone(_resolve_reasoning_effort({"reasoning_effort": "extreme"}, allowed))
        self.assertIsNone(_resolve_reasoning_effort({}, allowed))

    def test_resolve_reasoning_effort_disabled_when_feature_off(self) -> None:
        self.assertIsNone(_resolve_reasoning_effort({"reasoning_effort": "high"}, None))

    async def test_llm_node_binds_reasoning_effort_when_allowed(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
            reasoning_effort=_effort_settings(frozenset({"low", "high"})),
        )

        await node(
            {
                "messages": [HumanMessage(content="hi")],
                "reasoning_effort": "high",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(llm.bound_kwargs[-1]["reasoning_effort"], "high")

    async def test_llm_node_ignores_reasoning_effort_outside_allow_list(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[_tool("get_current_time")],
            system_text="system",
            max_tokens=4000,
            reasoning_effort=_effort_settings(frozenset({"low", "high"})),
        )

        await node(
            {
                "messages": [HumanMessage(content="hi")],
                "reasoning_effort": "extreme",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertNotIn("reasoning_effort", llm.bound_kwargs[-1])

    async def test_llm_node_binds_reasoning_effort_without_active_tools(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[],
            system_text="system",
            max_tokens=4000,
            reasoning_effort=_effort_settings(frozenset({"high"})),
        )

        await node(
            {
                "messages": [HumanMessage(content="hi")],
                "reasoning_effort": "high",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(llm.plain_bind_kwargs[-1], {"reasoning_effort": "high"})
        self.assertEqual(llm.invocations[0][0], "bound")

    async def test_llm_node_stays_unbound_when_feature_disabled(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[],
            system_text="system",
            max_tokens=4000,
        )

        await node(
            {
                "messages": [HumanMessage(content="hi")],
                "reasoning_effort": "high",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(llm.plain_bind_kwargs, [])
        self.assertEqual(llm.invocations[0][0], "plain")

    async def test_llm_node_uses_qwen_style_enable_thinking(self) -> None:
        llm = _FakeLLM()
        node = _make_llm_node(
            llm,
            tools=[],
            system_text="system",
            max_tokens=4000,
            reasoning_effort=_effort_settings(frozenset({"none", "high"}), style="qwen"),
        )

        await node(
            {
                "messages": [HumanMessage(content="hi")],
                "reasoning_effort": "none",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertNotIn("reasoning_effort", llm.plain_bind_kwargs[-1])
        self.assertEqual(
            llm.plain_bind_kwargs[-1],
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        )

    async def test_custom_analysis_node_binds_reasoning_effort(self) -> None:
        llm = _FakeLLM()
        node = make_analysis_node(
            llm,
            [_tool("run_code")],
            4000,
            reasoning_effort=_effort_settings(frozenset({"medium"})),
        )

        await node(
            {
                "messages": [HumanMessage(content="analyze")],
                "reasoning_effort": "medium",
            },
            {"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(llm.bound_kwargs[-1]["reasoning_effort"], "medium")


class RouterObservabilityTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_router_logs_intent_and_thread_without_message_content(self) -> None:
        _enable_log_capture("wotbot.agent.nodes")

        router = make_router_node(_FakeRouterLLM(), max_tokens=4000)

        with self.assertLogs("wotbot.agent.nodes", level="INFO") as logs:
            result = await router(
                {"messages": [HumanMessage(content="secret router text")]},
                {"configurable": {"thread_id": "thread-router"}},
            )

        output = "\n".join(logs.output)
        self.assertEqual(result, {"intent": "analysis"})
        self.assertIn("Router classified intent", output)
        self.assertIn("thread_id=thread-router", output)
        self.assertIn("intent=analysis", output)
        self.assertNotIn("secret router text", output)

    async def test_router_suppresses_internal_llm_stream_events(self) -> None:
        llm = _FakeRouterLLM()
        router = make_router_node(llm, max_tokens=4000)
        config = {
            "configurable": {"thread_id": "thread-router"},
            "metadata": {"existing": "preserved"},
        }

        await router(
            {"messages": [HumanMessage(content="route this")]},
            config,
        )

        self.assertEqual(
            llm.structured.configs,
            [
                {
                    "configurable": {"thread_id": "thread-router"},
                    "metadata": {"existing": "preserved"},
                    # LangGraph suppresses tagged runs in "messages" mode.
                    "tags": ["nostream"],
                }
            ],
        )
        self.assertEqual(config["metadata"], {"existing": "preserved"})


class VoiceResponseInstructionsTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_response_instructions_reach_every_foreground_branch(self) -> None:
        marker = "VOICE RESPONSE MARKER"
        factories = (
            make_respond_node,
            make_control_node,
            make_analysis_node,
            make_jobs_node,
            make_virtual_things_node,
        )

        for factory in factories:
            with self.subTest(factory=factory.__name__):
                llm = _FakeLLM()
                node = factory(
                    llm,
                    [],
                    4000,
                    response_instructions=f"\n{marker}\n",
                )

                await node(
                    {"messages": [HumanMessage(content="hello")]},
                    {"configurable": {"thread_id": "thread-voice"}},
                )

                messages = llm.invocations[0][1]
                self.assertIn(marker, messages[0].content)


class StableSystemPromptTestCase(unittest.IsolatedAsyncioTestCase):
    """Foreground system prompts must be byte-identical across invocations.

    A value that changes per call (previously the millisecond timestamp) makes
    the whole prompt prefix -- history and any attached camera frame included --
    unique every time, so no provider prefix cache can hit. The current time now
    comes from the get_current_time tool, whose result is appended to history
    instead of rewriting the prefix.
    """

    async def test_system_prompt_is_identical_across_calls(self) -> None:
        factories = (
            make_respond_node,
            make_control_node,
            make_analysis_node,
            make_jobs_node,
            make_virtual_things_node,
        )

        for factory in factories:
            with self.subTest(factory=factory.__name__):
                llm = _FakeLLM()
                node = factory(llm, [], 4000)
                state = {"messages": [HumanMessage(content="hello")]}
                config = {"configurable": {"thread_id": "thread-cache"}}

                await node(state, config)
                await node(state, config)

                first, second = (invocation[1] for invocation in llm.invocations[:2])
                self.assertEqual(first[0].content, second[0].content)
                self.assertNotIn("## Current Time", first[0].content)
                self.assertNotIn("now_ts_ms", first[0].content)

    async def test_prior_analysis_only_changes_when_run_code_output_changes(self) -> None:
        llm = _FakeLLM()
        node = make_virtual_things_node(llm, [], 4000)
        run_code = AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "run_code", "args": {"code": "x = 1"}}],
        )
        state = {"messages": [HumanMessage(content="hi"), run_code]}

        await node(state, None)
        await node(state, None)

        first, second = (invocation[1] for invocation in llm.invocations[:2])
        self.assertEqual(first[0].content, second[0].content)
        self.assertIn("## Prior Analysis Code", first[0].content)


class PriorAnalysisBlockTestCase(unittest.TestCase):
    def _run_code_message(self, code: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "run_code", "args": {"code": code}}],
        )

    def test_returns_latest_run_code_source(self) -> None:
        messages = [
            HumanMessage(content="analyze"),
            self._run_code_message("print('first')"),
            ToolMessage(content="ok", tool_call_id="c1"),
            self._run_code_message("print('second')"),
            ToolMessage(content="ok", tool_call_id="c1"),
        ]

        self.assertEqual(_latest_run_code_source(messages), "print('second')")

    def test_ignores_non_run_code_tool_calls(self) -> None:
        messages = [
            AIMessage(
                content="",
                tool_calls=[{"id": "t1", "name": "things_search", "args": {"q": "meter"}}],
            ),
        ]

        self.assertIsNone(_latest_run_code_source(messages))
        self.assertEqual(_prior_analysis_block(messages), "")

    def test_block_embeds_source_when_present(self) -> None:
        block = _prior_analysis_block([self._run_code_message("x = 1")])

        self.assertIn("## Prior Analysis Code", block)
        self.assertIn("x = 1", block)


if __name__ == "__main__":
    unittest.main()
