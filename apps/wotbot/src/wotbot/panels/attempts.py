"""Repair accounting from checkpointed tool results, never model-supplied IDs."""

import json
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

DEFAULT_MAX_REPAIRS = 2
TOOL_NAME = "create_web_interface"


@dataclass
class Attempt:
    number: int = 1
    previous_reports: list[str] = field(default_factory=list)
    blocked: str | None = None
    max_repairs: int = DEFAULT_MAX_REPAIRS

    def metadata(self, *, retry_allowed: bool = False) -> dict:
        return {
            "attempt": self.number,
            "max_repairs": self.max_repairs,
            "repairs_remaining": max(0, self.max_repairs + 1 - self.number),
            "retry_allowed": retry_allowed and self.number <= self.max_repairs,
            "previous_reports": self.previous_reports,
        }


def current_attempt(
    messages: list, tool_call_id: str, max_repairs: int = DEFAULT_MAX_REPAIRS
) -> Attempt:
    attempt = Attempt(max_repairs=max_repairs)
    calls = set()
    for message in messages:
        if isinstance(message, HumanMessage):
            attempt = Attempt(max_repairs=max_repairs)
            calls.clear()
        elif isinstance(message, AIMessage):
            panels = [call for call in message.tool_calls if call["name"] == TOOL_NAME]
            calls.update(call["id"] for call in panels)
            if any(call["id"] == tool_call_id for call in panels[1:]):
                return Attempt(blocked="parallel", max_repairs=max_repairs)
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
                attempt = Attempt(max_repairs=max_repairs)
                continue
            validation = result.get("browser_validation", {})
            report_id = validation.get("report_id")
            if isinstance(report_id, str):
                attempt.previous_reports.append(report_id)
            attempt.number += 1
            if validation.get("status") in {"unavailable", "inconclusive"}:
                attempt.blocked = "inconclusive"
            if attempt.number > max_repairs + 1:
                attempt.blocked = attempt.blocked or "exhausted"
    return attempt
