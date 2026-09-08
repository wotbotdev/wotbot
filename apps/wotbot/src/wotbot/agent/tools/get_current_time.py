"""Tool to get the current time and date in various formats."""

import time
from datetime import datetime

from wotbot.agent.tools.contracts import tool


@tool
def get_current_time() -> str:
    """Get the current time and date.

    Returns:
        A string containing the ISO date/time, timezone, weekday,
        month, and timestamps in seconds and milliseconds.
    """
    now = datetime.now().astimezone()

    # Weekday and Month in natural language
    weekday = now.strftime("%A")
    month = now.strftime("%B")

    # Timestamps
    ts_seconds = int(time.time())
    ts_milliseconds = int(time.time() * 1000)

    return (
        f"ISO: {now.isoformat()}\n"
        f"Timezone: {now.tzname()} ({now.strftime('%z')})\n"
        f"Weekday: {weekday}\n"
        f"Month: {month}\n"
        f"Timestamp (s): {ts_seconds}\n"
        f"Timestamp (ms): {ts_milliseconds}"
    )
