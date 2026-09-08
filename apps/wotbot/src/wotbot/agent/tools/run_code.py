"""LangChain tool for executing Python code in an isolated session."""

import httpx
from langchain_core.runnables import RunnableConfig

from wotbot.agent.tools.contracts import tool
from wotbot.clients.code_executor import (
    CodeExecutionUncertainError,
    CodeExecutorClient,
    format_code_execution_result,
)
from wotbot.core.settings import Settings

_settings = Settings()
_code_executor_client = CodeExecutorClient(_settings)


@tool
async def run_code(code: str, config: RunnableConfig) -> dict:
    """Execute Python code in an isolated Python session.

    Use for multiple Thing reads, history, joins, calculations or static charts.
    Inspect affordances first. import wot exposes synchronous read_property,
    write_property and invoke_action calls; do not await them. pandas, numpy,
    matplotlib and plotly are available. Imports and variables persist until
    the session is evicted; jobs and virtual handlers use separate sessions.

    Keep datasets in Python and print summaries or small samples. Capture charts
    with fig.show() or plt.show(), images with save_image(image), and export files
    with save_artifact(bytes_or_text_or_binary_stream, filename="data.csv",
    mime_type="text/csv"). Serialize tables explicitly, e.g. df.to_csv(index=False).
    Files publish only after successful execution. Results include file metadata
    and expiry; the UI provides downloads. Exports expire after seven days by
    default, charts after one hour; deployment settings can change these limits.
    Reading does not extend retention. Refer to charts or download filenames
    naturally; never reconstruct file contents in the conversation.

    Failed code may have performed earlier device actions. Inspect the returned
    interactions and current device state before retrying any actions.
    """
    chat_id = config.get("configurable", {}).get("thread_id", "default")
    try:
        response = await _code_executor_client.execute(session_id=chat_id, code=code)
        return format_code_execution_result(response)
    except CodeExecutionUncertainError as exc:
        return {"ok": False, "error": str(exc)}
    except httpx.ConnectError:
        return {"error": "Code executor service is unavailable. Please try again later."}
    except httpx.TimeoutException:
        return {
            "error": (
                f"Code executor request timed out after "
                f"{_settings.code_executor_timeout_seconds} seconds."
            )
        }
    except httpx.HTTPStatusError as e:
        detail = None
        try:
            detail = e.response.json().get("detail")
        except Exception:
            detail = None
        if detail:
            return {"error": detail}
        return {"error": f"Code execution failed with status {e.response.status_code}."}
