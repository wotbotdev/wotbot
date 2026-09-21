"""Validation evidence shared by the browser client, storage and visual review."""

import base64
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wotbot.panels.browser_protocol import UNTESTED_OPERATIONS

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024


class BrowserValidation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["passed", "failed", "inconclusive", "unavailable"]
    diagnostics: list[dict[str, str]] = Field(default_factory=list)
    checks: dict | None = None
    screenshot_base64: str | None = Field(default=None, exclude=True)
    narrow_screenshot_base64: str | None = Field(default=None, exclude=True)
    visual_review: dict | None = None
    read_count: int | None = None
    untested_operations: list[str] = Field(default_factory=lambda: list(UNTESTED_OPERATIONS))


def checks_passed(verdict: object) -> bool:
    """A status label alone is not evidence of any completed assertions."""
    if not isinstance(verdict, dict) or verdict.get("status") != "passed":
        return False
    checks = verdict.get("checks")
    return (
        isinstance(checks, list)
        and bool(checks)
        and all(
            isinstance(check, dict)
            and isinstance(check.get("label"), str)
            and check.get("passed") is True
            and check.get("error") is None
            for check in checks
        )
    )


def decode_screenshot(encoded: str | None) -> bytes | None:
    if encoded and len(encoded) > (MAX_SCREENSHOT_BYTES + 2) // 3 * 4:
        raise ValueError("Validation screenshot exceeds its storage limit")
    screenshot = base64.b64decode(encoded, validate=True) if encoded else None
    if screenshot and (
        len(screenshot) > MAX_SCREENSHOT_BYTES or not screenshot.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise ValueError("Invalid validation screenshot")
    return screenshot
