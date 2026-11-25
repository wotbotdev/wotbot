import hashlib
import json
from typing import Any

from sqlalchemy import Text, cast, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wotbot.catalog.models import (
    Thing,
    ThingConflictError,
    ThingDocument,
    ThingRecord,
)
from wotbot.core.time import utc_now


def sanitize_document(document: ThingDocument) -> ThingDocument:
    return {
        key: value for key, value in document.items() if key not in {"hash", "source", "origin"}
    }


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    tags: list[str] = []
    for item in value:
        if isinstance(item, (str, int, float, bool)):
            tags.append(str(item))
    return tags


def summarize_document(document: ThingDocument) -> tuple[str, str, list[str], str]:
    thing_id = document.get("id")
    if not isinstance(thing_id, str) or not thing_id.strip():
        raise ValueError("Thing Description is missing a valid 'id'")

    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        title = thing_id

    description = document.get("description")
    if not isinstance(description, str):
        description = ""

    return thing_id, title, _normalize_tags(document.get("tags")), description


def hash_document(document: ThingDocument) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_record(thing: Thing) -> ThingRecord:
    return ThingRecord(
        id=thing.id,
        title=thing.title,
        description=thing.description,
        tags=list(thing.tags or []),
        origin_kind=thing.origin_kind,
        origin_provider=thing.origin_provider,
        origin_external_id=thing.origin_external_id,
        origin_source_id=thing.origin_source_id,
        document=dict(thing.document),
        document_hash=thing.document_hash,
    )


def serialize_document(record: ThingRecord) -> ThingDocument:
    return dict(record.document)


def list_things(
    session: Session,
    *,
    query: str = "",
    page: int = 1,
    per_page: int = 25,
    origin_kind: str | None = None,
) -> tuple[list[ThingRecord], int]:
    normalized_query = query.strip().lower()
    offset = (page - 1) * per_page
    filters = []

    if normalized_query:
        pattern = f"%{normalized_query}%"
        filters.append(
            or_(
                func.lower(Thing.id).like(pattern),
                func.lower(Thing.title).like(pattern),
                func.lower(Thing.description).like(pattern),
                func.lower(cast(Thing.document, Text)).like(pattern),
            )
        )

    if origin_kind is not None:
        filters.append(Thing.origin_kind == origin_kind)
    count_query = select(func.count()).select_from(Thing).where(*filters)
    total = int(session.scalar(count_query) or 0)

    things = session.scalars(
        select(Thing)
        .where(*filters)
        .order_by(func.lower(Thing.title), Thing.id)
        .offset(offset)
        .limit(per_page)
    ).all()

    return [to_record(thing) for thing in things], total


def get_thing(session: Session, thing_id: str) -> ThingRecord | None:
    thing = session.get(Thing, thing_id)
    return to_record(thing) if thing is not None else None


def _thing_values(
    document: ThingDocument,
    *,
    include_created_at: bool,
    origin_kind: str = "manual",
    origin_provider: str | None = None,
    origin_external_id: str | None = None,
    origin_source_id: str | None = None,
) -> dict[str, Any]:
    thing_id, title, tags, description = summarize_document(document)
    now = utc_now()
    values: dict[str, Any] = {
        "id": thing_id,
        "title": title,
        "description": description,
        "tags": tags,
        "origin_kind": origin_kind,
        "origin_provider": origin_provider,
        "origin_external_id": origin_external_id,
        "origin_source_id": origin_source_id,
        "document": document,
        "document_hash": hash_document(document),
        "updated_at": now,
    }
    if include_created_at:
        values["created_at"] = now
    return values


def create_thing(
    session: Session,
    document: ThingDocument,
    *,
    origin_kind: str = "manual",
    origin_provider: str | None = None,
    origin_external_id: str | None = None,
    origin_source_id: str | None = None,
) -> ThingRecord:
    sanitized = sanitize_document(document)
    thing = Thing(
        **_thing_values(
            sanitized,
            include_created_at=True,
            origin_kind=origin_kind,
            origin_provider=origin_provider,
            origin_external_id=origin_external_id,
            origin_source_id=origin_source_id,
        )
    )
    session.add(thing)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ThingConflictError(f"Thing '{thing.id}' already exists") from exc

    return to_record(thing)


def put_thing(session: Session, thing_id: str, document: ThingDocument) -> tuple[ThingRecord, bool]:
    sanitized = sanitize_document(document)
    document_id = summarize_document(sanitized)[0]
    if document_id != thing_id:
        raise ValueError("Thing id in path and document body must match")

    stmt = insert(Thing).values(**_thing_values(sanitized, include_created_at=True))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Thing.id],
        set_={
            "title": stmt.excluded.title,
            "description": stmt.excluded.description,
            "tags": stmt.excluded.tags,
            "document": stmt.excluded.document,
            "document_hash": stmt.excluded.document_hash,
            "updated_at": stmt.excluded.updated_at,
        },
        # Thing IDs are global; never overwrite a Thing owned by another workspace.
        where=Thing.workspace_id == stmt.excluded.workspace_id,
    ).returning(Thing, literal_column("xmax = 0").label("inserted"))

    row = session.execute(
        stmt,
        execution_options={"populate_existing": True},
    ).one_or_none()
    if row is None:
        raise ThingConflictError(f"Thing '{thing_id}' already exists in another workspace")
    thing = row[0]
    return to_record(thing), bool(row.inserted)


def get_thing_by_origin(
    session: Session,
    *,
    provider: str,
    external_id: str,
    source_id: str,
) -> ThingRecord | None:
    thing = session.scalar(
        select(Thing).where(
            Thing.origin_kind == "discovery",
            Thing.origin_provider == provider,
            Thing.origin_external_id == external_id,
            Thing.origin_source_id == source_id,
        )
    )
    return to_record(thing) if thing is not None else None


def count_source_dependents(session: Session, source_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Thing)
            .where(
                Thing.origin_kind == "discovery",
                Thing.origin_source_id == source_id,
            )
        )
        or 0
    )


def delete_thing(session: Session, thing_id: str) -> bool:
    thing = session.get(Thing, thing_id)
    if thing is None:
        return False

    session.delete(thing)
    session.flush()
    return True
