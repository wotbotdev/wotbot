"""Bounded JSON attachments delivered to panels without model-mediated copying."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, true
from sqlalchemy.orm import Session

from wotbot.clients.code_executor import CodeExecutorClient
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.panels.models import Panel, PanelData, PanelVersion

MAX_ATTACHMENTS = 8
MAX_DATA_BYTES = 8 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")


def validate_data_refs(data: dict[str, str] | None) -> dict[str, str]:
    refs = dict(data or {})
    if len(refs) > MAX_ATTACHMENTS:
        raise ValueError(f"At most {MAX_ATTACHMENTS} panel data attachments are allowed")
    for name, ref in refs.items():
        if not _NAME.fullmatch(name) or name in {"constructor", "prototype"}:
            raise ValueError(
                "Attachment names must start with a letter and use letters, digits, _ or -"
            )
        if not isinstance(ref, str) or not _ID.fullmatch(ref) or ".." in ref:
            raise ValueError(f"Invalid artifact or snapshot ID for attachment {name}")
    return refs


def parse_json(content: str) -> Any:
    def invalid_constant(value):
        raise ValueError("Panel data must be strict JSON with finite numbers")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            return invalid_constant(value)
        return number

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Panel data must not contain duplicate object keys")
            result[key] = value
        return result

    try:
        return json.loads(
            content,
            parse_constant=invalid_constant,
            parse_float=finite_float,
            object_pairs_hook=unique_object,
        )
    except (RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Panel data must be valid JSON") from exc


def load_data(
    session: Session, data: dict[str, str] | None, *, retain=False
) -> dict[str, PanelData]:
    refs = validate_data_refs(data)
    if not refs:
        return {}
    query = select(PanelData).where(PanelData.id.in_(set(refs.values()))).order_by(PanelData.id)
    if retain:
        # Serialize retention against cleanup. Always lock in ID order.
        query = query.with_for_update()
    found = {row.id: row for row in session.scalars(query)}
    rows = {}
    total = 0
    for name, identifier in refs.items():
        row = found.get(identifier)
        if row is None or (row.expires_at is not None and row.expires_at <= utc_now()):
            raise ValueError(f"Panel data attachment {name} is missing or expired")
        total += row.size_bytes
        if total > MAX_DATA_BYTES:
            raise ValueError("Panel data exceeds the 8 MiB attachment limit")
        rows[name] = row
    if retain:
        for row in rows.values():
            row.expires_at = None
    return rows


def data_metadata(rows: dict[str, PanelData]) -> dict[str, dict]:
    return {
        name: {
            "id": row.id,
            "artifact_id": row.artifact_id,
            "filename": row.filename,
            "mime_type": row.mime_type,
            "sha256": row.sha256,
            "size_bytes": row.size_bytes,
        }
        for name, row in rows.items()
    }


def data_contents(session: Session, data: dict[str, str] | None) -> dict[str, str]:
    return {name: row.content for name, row in load_data(session, data).items()}


def prune_data(session: Session, candidates: set[str] | None = None) -> None:
    """Remove expired temporary snapshots or detached, formerly pinned snapshots."""
    selection = select(PanelData.id)
    selection = (
        selection.where(PanelData.id.in_(candidates))
        if candidates is not None
        else selection.where(PanelData.expires_at <= utc_now())
    )
    locked = list(
        session.scalars(selection.order_by(PanelData.id).with_for_update(skip_locked=True))
    )
    if not locked:
        return
    # Check references after taking the locks so a concurrent pin cannot lose
    # its snapshot to a stale reference check.
    statement = delete(PanelData).where(PanelData.id.in_(locked))
    for model in (Panel, PanelVersion):
        values = func.jsonb_each_text(model.data).table_valued("key", "value")
        referenced = (
            select(1).select_from(model).join(values, true()).where(values.c.value == PanelData.id)
        )
        statement = statement.where(~referenced.exists())
    session.execute(statement.execution_options(synchronize_session=False))


async def _snapshot_export(
    name: str, identifier: str, client: CodeExecutorClient, *, max_bytes: int
) -> PanelData:
    try:
        descriptor = await client.artifact_metadata(identifier)
        mime = str(descriptor.get("mime_type") or "").split(";", 1)[0].strip().lower()
        if mime not in {"application/json", "application/geo+json"}:
            raise ValueError(
                f"Attachment {name} must be exported as application/json or application/geo+json"
            )
        size = descriptor.get("size_bytes")
        if type(size) is not int or size < 0 or size > max_bytes:
            raise ValueError("Panel data exceeds the 8 MiB attachment limit")
        raw = await client.read_artifact(identifier, max_bytes=max_bytes)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in {404, 410}:
            raise
        raise ValueError(
            f"Attachment {name} ({identifier}) is missing or expired. "
            "Use the exact exported artifact ID or export the data again."
        ) from exc

    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != size or digest != descriptor.get("sha256"):
        raise ValueError(f"Attachment {name} does not match its artifact checksum or size")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Attachment {name} must be UTF-8 JSON") from exc
    parse_json(content)
    try:
        expiry = datetime.fromisoformat(descriptor["expires_at"])
        if expiry.tzinfo is None or expiry <= utc_now():
            raise ValueError("expired")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Attachment {name} has no valid artifact expiry") from exc
    return PanelData(
        id="panel-data-" + uuid4().hex,
        artifact_id=identifier,
        filename=descriptor["filename"],
        mime_type=mime,
        sha256=digest,
        size_bytes=size,
        content=content,
        expires_at=expiry,
    )


async def resolve_data(
    data: dict[str, str] | None, client: CodeExecutorClient, *, session_factory=None
) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """Resolve export IDs once; existing snapshot IDs are reused during edits."""
    refs = validate_data_refs(data)
    if not refs:
        return {}, {}, {}
    sessions = session_factory or get_session_factory()

    def existing():
        with sessions() as session:
            rows = load_data(
                session, {k: v for k, v in refs.items() if v.startswith("panel-data-")}
            )
            contents = {name: row.content for name, row in rows.items()}
            return contents, data_metadata(rows)

    contents, metadata = await asyncio.to_thread(existing)
    total = sum(item["size_bytes"] for item in metadata.values())
    new_rows = []
    for name, identifier in refs.items():
        if name in contents:
            continue
        row = await _snapshot_export(name, identifier, client, max_bytes=MAX_DATA_BYTES - total)
        new_rows.append(row)
        contents[name] = row.content
        metadata.update(data_metadata({name: row}))
        total += row.size_bytes

    def persist():
        with sessions() as session:
            prune_data(session)
            session.add_all(new_rows)
            session.commit()

    if new_rows:
        await asyncio.to_thread(persist)
    return {name: item["id"] for name, item in metadata.items()}, contents, metadata
