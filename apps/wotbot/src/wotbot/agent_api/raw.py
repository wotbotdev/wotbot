"""One checkpointed tool call, with deterministic completion and no LLM."""

import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from wotbot.agent_api.catalog import public_tools, validate_arguments
from wotbot.agent_api.types import Operation


class RawState(MessagesState):
    raw_result: Any
    raw_error: str | None


def result_error(result):
    if isinstance(result, dict):
        if (
            result.get("error")
            or result.get("ok") is False
            or result.get("status") in {"error", "failed", "stopped", "credential_cancelled"}
        ):
            return str(
                result.get("error") or "Tool execution failed; actions may already have occurred"
            )
    return None


def build_raw_graph(checkpointer, *, subscriptions=None, tools=None):
    catalog = tools if tools is not None else public_tools()

    async def prepare(state, config: RunnableConfig):
        operation = Operation.model_validate(config["configurable"]["operation"])
        return {
            "raw_result": None,
            "raw_error": None,
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": config["configurable"]["task_id"] + ":raw",
                            "name": operation.name,
                            "args": operation.arguments,
                            "type": "tool_call",
                        }
                    ],
                )
            ],
        }

    async def execute(state, config: RunnableConfig):
        call = state["messages"][-1].tool_calls[0]
        try:
            if tools is None:
                validate_arguments(call["name"], call["args"])
            replies = config["configurable"].get("resume_responses") or {}
            cancelled = any(
                isinstance(reply, dict) and reply.get("status") == "cancelled"
                for reply in replies.values()
            )
            if cancelled:
                result = {
                    "status": "credential_cancelled",
                    "error": "The pending request was cancelled; the tool was not retried",
                }
            elif subscriptions and call["name"] in subscriptions.TOOLS:
                result = await subscriptions.execute(call["name"], call["args"], config)
            else:
                result = await catalog[call["name"]].ainvoke(call, config=config)
                if isinstance(result, ToolMessage):
                    if result.status == "error":
                        result = {"error": str(result.content)}
                    else:
                        try:
                            result = (
                                json.loads(result.content)
                                if isinstance(result.content, str)
                                else result.content
                            )
                        except ValueError:
                            result = result.content
            if isinstance(result, dict) and result.get("status") in {
                "credential_required",
                "credential_rejected",
            }:
                interrupt({"kind": "credential", **result})
                # Resume re-enters execute. A repeated rejection must not loop.
                result = {
                    "error": "Credentials were cancelled or rejected again; no automatic retry was made"
                }
            error = result_error(result)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            result, error = {"error": str(exc)}, str(exc)
        return {
            "raw_result": result,
            "raw_error": error,
            "messages": [
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=call["id"],
                    status="error" if error else "success",
                )
            ],
        }

    async def finish(state):
        return {
            "messages": [AIMessage(content=state.get("raw_error") or "Tool execution completed.")]
        }

    graph = StateGraph(RawState)
    graph.add_node("prepare", prepare)
    graph.add_node("execute", execute)
    graph.add_node("finish", finish)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "execute")
    graph.add_edge("execute", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
