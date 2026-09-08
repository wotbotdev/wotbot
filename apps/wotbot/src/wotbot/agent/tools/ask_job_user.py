"""Worker-only tool for pausing a background job for human input."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from wotbot.agent.tools.contracts import tool


@tool
async def ask_job_user(
    question: str,
    config: RunnableConfig,
    context: str | None = None,
) -> dict[str, str | None]:
    """Ask the job owner for input and pause the background job.

    Use this only from a background job when the job cannot safely continue
    without user input. The graph pauses until the job owner replies; after
    resume, this tool returns the reply so the agent can finish the same run.
    """
    configurable = config.get("configurable", {})
    clean_question = question.strip()
    answer = interrupt(
        {
            "kind": "job_user_input",
            "question": clean_question,
            "context": context,
            "job_id": configurable.get("job_id"),
            "run_id": configurable.get("run_id"),
            "thread_id": configurable.get("thread_id"),
        }
    )
    return {
        "status": "input_received",
        "question": clean_question,
        "answer": str(answer),
        "context": context,
        "job_id": configurable.get("job_id"),
        "run_id": configurable.get("run_id"),
        "thread_id": configurable.get("thread_id"),
    }
