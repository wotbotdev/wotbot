"""The chat tool preserves execution failures and actionable recovery guidance."""

import asyncio
from unittest.mock import AsyncMock, patch

from wotbot.agent.tools.run_code import run_code
from wotbot.clients.code_executor import CodeExecutionUncertainError


def test_failed_execution_exposes_completed_actions_to_chat():
    completed = {
        "type": "invoke_action",
        "thing_id": "urn:test:device",
        "name": "toggle",
        "ok": True,
    }
    response = {
        "ok": False,
        "error": "ValueError: after action",
        "stdout": "output ... truncated",
        "wot_calls": [completed],
        "images": [],
        "plotly": [],
    }
    with patch(
        "wotbot.agent.tools.run_code._code_executor_client.execute",
        new=AsyncMock(return_value=response),
    ) as execute:
        result = asyncio.run(
            run_code.ainvoke(
                {"code": "toggle(); raise ValueError('after action')"},
                config={"configurable": {"thread_id": "chat-test"}},
            )
        )
    assert result["ok"] is False
    assert result["error"] == "ValueError: after action"
    assert result["wot_calls"] == [completed]
    assert execute.await_args.kwargs["session_id"] == "chat-test"


def test_uncertain_execution_returns_guidance_without_replaying():
    with patch(
        "wotbot.agent.tools.run_code._code_executor_client.execute",
        new=AsyncMock(side_effect=CodeExecutionUncertainError()),
    ) as execute:
        result = asyncio.run(run_code.ainvoke({"code": "toggle()"}))
    assert result["ok"] is False
    assert "may already have applied" in result["error"]
    assert "Inspect current device state" in result["error"]
    execute.assert_awaited_once()
