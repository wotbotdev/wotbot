import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from wotbot.panels.attempts import current_attempt
from wotbot.panels.evidence import BrowserValidation


def call(identifier="call", title="Map"):
    return {
        "name": "create_web_interface",
        "id": identifier,
        "args": {
            "html": "<h1>Map</h1>",
            "title": title,
            "data": {"map": "panel-data-example"},
        },
    }


def failed(identifier="call", status="failed"):
    return ToolMessage(
        name="create_web_interface",
        tool_call_id=identifier,
        content=json.dumps(
            {
                "error": "Failed",
                "browser_validation": {
                    "status": status,
                    "report_id": identifier.zfill(32),
                },
            }
        ),
    )


def history(statuses):
    messages = [HumanMessage(content="Create a map")]
    for i, status in enumerate(statuses):
        messages += [AIMessage(content="", tool_calls=[call(str(i))]), failed(str(i), status)]
    return messages


def test_three_attempts_then_stop_despite_changed_title_or_html():
    for failures, expected in [(0, None), (1, None), (2, None), (3, "exhausted")]:
        attempt = current_attempt(history(["failed"] * failures), "next")
        assert attempt.number == failures + 1
        assert attempt.blocked == expected
    assert (
        current_attempt(history(["failed"] * 2), "next").metadata(retry_allowed=True)[
            "retry_allowed"
        ]
        is False
    )


@pytest.mark.parametrize("status", ["inconclusive", "unavailable"])
def test_outage_stops_repairs_until_a_new_user_request(status):
    messages = history([status])
    assert current_attempt(messages, "next").blocked == "inconclusive"
    fresh = current_attempt(messages + [HumanMessage(content="Try again now")], "next")
    assert fresh.number == 1 and fresh.blocked is None and fresh.previous_reports == []


def test_success_allows_a_separate_panel_with_its_own_repair_budget():
    messages = history(["failed", "failed"])
    messages += [
        AIMessage(content="", tool_calls=[call("success")]),
        ToolMessage(
            tool_call_id="success",
            content=json.dumps({"artifacts": [{"kind": "web"}]}),
        ),
    ]
    assert current_attempt(messages, "next").number == 1


def test_parallel_calls_cannot_race_the_budget():
    messages = [
        HumanMessage(content="Create panels"),
        AIMessage(content="", tool_calls=[call("a"), call("b")]),
    ]
    assert current_attempt(messages, "a").blocked is None
    assert current_attempt(messages, "b").blocked == "parallel"


def test_runtime_is_hidden_from_the_model_tool_schema():
    from wotbot.agent.tools.create_web_interface import create_web_interface

    assert set(create_web_interface.tool_call_schema["properties"]) == {
        "html",
        "capabilities",
        "title",
        "data",
    }


@pytest.mark.anyio
async def test_real_tool_node_injects_history_and_rejects_fourth_attempt():
    import importlib

    module = importlib.import_module("wotbot.agent.tools.create_web_interface")
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([module.create_web_interface]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    graph = graph.compile()
    messages = history(["failed", "failed", "failed"])
    messages.append(AIMessage(content="", tool_calls=[call("fourth", "Renamed map")]))
    with (
        patch.object(module, "validate_in_browser", AsyncMock()) as browser,
        patch.object(module, "save_report", AsyncMock()) as store,
    ):
        result = await graph.ainvoke({"messages": messages})
    output = json.loads(result["messages"][-1].content)
    assert "repair limit reached" in output["error"]
    assert output["browser_validation"]["retry_allowed"] is False
    browser.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.anyio
async def test_last_allowed_attempt_keeps_evidence_and_passes_without_extra_images_in_context():
    import importlib

    module = importlib.import_module("wotbot.agent.tools.create_web_interface")
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([module.create_web_interface]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    graph = graph.compile()
    messages = history(["failed", "failed"]) + [AIMessage(content="", tool_calls=[call("third")])]
    saved = AsyncMock(
        side_effect=lambda **kw: {**kw["report"], "report_id": "c" * 32, "has_screenshot": True}
    )
    with (
        patch.object(module, "_check_panel", AsyncMock(return_value=[])),
        patch.object(module, "resolve_data", AsyncMock(return_value=({}, {}, {}))),
        patch.object(
            module,
            "validate_in_browser",
            AsyncMock(
                return_value=BrowserValidation(
                    status="passed", checks={"status": "passed"}, screenshot_base64="png-image"
                )
            ),
        ),
        patch.object(module, "save_report", saved),
        patch.object(
            module._code_executor_client, "store_web_artifact", AsyncMock(return_value="panel.html")
        ),
    ):
        result = await graph.ainvoke({"messages": messages})
    output = json.loads(result["messages"][-1].content)
    assert output["browser_validation"]["attempt"] == 3
    assert len(output["browser_validation"]["previous_reports"]) == 2
    assert output["browser_validation"]["retry_allowed"] is False
    assert output["artifacts"][0]["filename"] == "panel.html"
    assert "png-image" not in result["messages"][-1].content
    assert saved.call_args.kwargs["screenshot_base64"] == "png-image"
