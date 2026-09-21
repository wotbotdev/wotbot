"""Repair accounting from checkpointed tool results, never model-supplied IDs."""

import json
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

MAX_REPAIRS = 2
TOOL_NAME = "create_web_interface"


@dataclass
class Attempt:
    number: int = 1
    previous_reports: list[str] = field(default_factory=list)
    blocked: str | None = None

    def metadata(self, *, retry_allowed: bool = False) -> dict:
        return {
            "attempt": self.number,
            "max_repairs": MAX_REPAIRS,
            "repairs_remaining": max(0, MAX_REPAIRS + 1 - self.number),
            "retry_allowed": retry_allowed and self.number <= MAX_REPAIRS,
            "previous_reports": self.previous_reports,
        }


def current_attempt(messages: list, tool_call_id: str) -> Attempt:
    attempt = Attempt()
    calls = set()
    for message in messages:
        if isinstance(message, HumanMessage):
            attempt = Attempt()
            calls.clear()
        elif isinstance(message, AIMessage):
            panels = [call for call in message.tool_calls if call["name"] == TOOL_NAME]
            calls.update(call["id"] for call in panels)
            if any(call["id"] == tool_call_id for call in panels[1:]):
                return Attempt(blocked="parallel")
        elif isinstance(message, ToolMessage) and message.tool_call_id in calls:
            try:
                result = json.loads(message.content)
            except (ValueError, TypeError):
                result = {"error": "Previous panel call failed"}
            if (
                not isinstance(result, dict)
                or result.get("panel_retry", {}).get("counted") is False
            ):
                continue
            if result.get("artifacts") and not result.get("error"):
                attempt = Attempt()
                continue
            validation = result.get("browser_validation", {})
            report_id = validation.get("report_id")
            if isinstance(report_id, str):
                attempt.previous_reports.append(report_id)
            attempt.number += 1
            if validation.get("status") in {"unavailable", "inconclusive"}:
                attempt.blocked = "inconclusive"
            if attempt.number > MAX_REPAIRS + 1:
                attempt.blocked = attempt.blocked or "exhausted"
    return attempt
