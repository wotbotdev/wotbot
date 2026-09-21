"""Persist bounded reports and serve evidence under existing panel read scopes."""

import asyncio
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.panels.evidence import decode_screenshot
from wotbot.panels.models import PanelValidationReport
from wotbot.threads.models import Thread, ThreadKind

RETENTION_DAYS = 30


async def save_report(
    *,
    title: str,
    document: str,
    thread_id: str | None,
    report: dict,
    screenshot_base64: str | None,
    narrow_screenshot_base64: str | None = None,
    session_factory=None,
) -> dict:
    if len(json.dumps(report).encode()) > 128 * 1024:
        raise ValueError("Validation report exceeds its storage limit")
    screenshot = decode_screenshot(screenshot_base64)
    narrow_screenshot = decode_screenshot(narrow_screenshot_base64)
    expires_at = utc_now() + timedelta(days=RETENTION_DAYS)
    identifier = uuid4().hex
    evidence = {
        **report,
        "report_id": identifier,
        "has_screenshot": screenshot is not None,
        "has_narrow_screenshot": narrow_screenshot is not None,
        "expires_at": expires_at.isoformat(),
    }

    def persist():
        with (session_factory or get_session_factory())() as session:
            # Panel-edit runs can use a temporary thread with no metadata row.
            thread = session.get(Thread, thread_id) if thread_id else None
            session.execute(
                delete(PanelValidationReport).where(PanelValidationReport.expires_at <= utc_now())
            )
            session.add(
                PanelValidationReport(
                    id=identifier,
                    thread_id=thread.id if thread else None,
                    title=title[:500],
                    document_sha256=hashlib.sha256(document.encode()).hexdigest(),
                    report=evidence,
                    screenshot=screenshot,
                    narrow_screenshot=narrow_screenshot,
                    expires_at=expires_at,
                )
            )
            session.commit()

    await asyncio.to_thread(persist)
    return evidence


def get_report(session: Session, report_id: str) -> PanelValidationReport:
    row = session.get(PanelValidationReport, report_id)
    if row is None or row.expires_at <= utc_now():
        raise HTTPException(status_code=404, detail="Validation report unavailable or expired")
    thread = session.get(Thread, row.thread_id) if row.thread_id else None
    # Match the chat routes: external conversations cannot be read through the
    # UI proxy using its shared credential, even when their IDs are known.
    if thread and thread.kind in {ThreadKind.A2A.value, ThreadKind.MCP_RAW.value}:
        raise HTTPException(status_code=404, detail="Validation report unavailable")
    return row
