from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Text, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from wotbot.catalog.models import Thing
from wotbot.core.time import utc_now
from wotbot.discovery.source_models import (
    DiscoverySource,
    DiscoverySourceCredential,
    SourceRecord,
)


def to_source_record(source: DiscoverySource) -> SourceRecord:
    return SourceRecord(
        id=source.id,
        provider=source.provider,
        external_id=source.external_id,
        title=source.title,
        description=source.description,
        tags=list(source.tags or []),
        config=dict(source.config or {}),
        network_access=source.network_access,
        security_name=source.security_name,
        security_scheme=source.security_scheme,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def get_source(session: Session, source_id: str) -> SourceRecord | None:
    source = session.get(DiscoverySource, source_id)
    return to_source_record(source) if source is not None else None


def get_source_by_identity(
    session: Session,
    *,
    provider: str,
    external_id: str,
) -> SourceRecord | None:
    source = session.scalar(
        select(DiscoverySource).where(
            DiscoverySource.provider == provider,
            DiscoverySource.external_id == external_id,
        )
    )
    return to_source_record(source) if source is not None else None


def list_sources(session: Session) -> list[SourceRecord]:
    sources = session.scalars(
        select(DiscoverySource).order_by(func.lower(DiscoverySource.title), DiscoverySource.id)
    ).all()
    return [to_source_record(source) for source in sources]


def insert_source(session: Session, record: SourceRecord) -> SourceRecord:
    source = DiscoverySource(
        id=record.id,
        provider=record.provider,
        external_id=record.external_id,
        title=record.title,
        description=record.description,
        tags=record.tags,
        config=record.config,
        network_access=record.network_access,
        security_name=record.security_name,
        security_scheme=record.security_scheme,
        created_at=record.created_at or utc_now(),
        updated_at=record.updated_at or utc_now(),
    )
    session.add(source)
    session.flush()
    return to_source_record(source)


def update_source(session: Session, record: SourceRecord) -> SourceRecord:
    source = session.get(DiscoverySource, record.id)
    if source is None:
        raise ValueError("Discovery source was not found")
    source.title = record.title
    source.description = record.description
    source.tags = record.tags
    source.config = record.config
    source.network_access = record.network_access
    source.security_name = record.security_name
    source.security_scheme = record.security_scheme
    source.updated_at = utc_now()
    session.flush()
    return to_source_record(source)


def count_source_dependents(session: Session, source_id: str) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Thing).where(Thing.origin_source_id == source_id)
        )
        or 0
    )


def delete_source(session: Session, source_id: str) -> bool:
    source = session.get(DiscoverySource, source_id)
    if source is None:
        return False
    session.delete(source)
    session.flush()
    return True


def set_source_credential(
    session: Session,
    *,
    source_id: str,
    security_name: str,
    scheme: str,
    credentials: dict[str, Any],
) -> None:
    now = utc_now()
    stmt = insert(DiscoverySourceCredential).values(
        id=str(uuid.uuid4()),
        source_id=source_id,
        security_name=security_name,
        scheme=scheme,
        credentials=credentials,
        created_at=now,
        updated_at=now,
    )
    upsert = stmt.on_conflict_do_update(
        constraint="uq_source_security",
        set_={
            "scheme": stmt.excluded.scheme,
            "credentials": stmt.excluded.credentials,
            "updated_at": stmt.excluded.updated_at,
        },
        # Never overwrite a credential owned by another workspace.
        where=DiscoverySourceCredential.workspace_id == stmt.excluded.workspace_id,
    ).returning(DiscoverySourceCredential.id)
    if session.execute(upsert).scalar_one_or_none() is None:
        raise ValueError("Discovery source was not found")
    session.flush()


def get_source_credential(
    session: Session,
    *,
    source_id: str,
    security_name: str,
) -> DiscoverySourceCredential | None:
    return session.scalar(
        select(DiscoverySourceCredential).where(
            DiscoverySourceCredential.source_id == source_id,
            DiscoverySourceCredential.security_name == security_name,
        )
    )


def list_source_credentials(session: Session, source_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(DiscoverySourceCredential)
        .where(DiscoverySourceCredential.source_id == source_id)
        .order_by(DiscoverySourceCredential.security_name)
    ).all()
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "security_name": row.security_name,
            "scheme": row.scheme,
            "has_credentials": bool(row.credentials),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


def delete_source_credential(
    session: Session,
    *,
    source_id: str,
    security_name: str,
) -> bool:
    result = session.execute(
        delete(DiscoverySourceCredential).where(
            DiscoverySourceCredential.source_id == source_id,
            DiscoverySourceCredential.security_name == security_name,
        )
    )
    session.flush()
    return bool(result.rowcount)


def delete_source_credentials(session: Session, source_id: str) -> None:
    session.execute(
        delete(DiscoverySourceCredential).where(DiscoverySourceCredential.source_id == source_id)
    )
    session.flush()


def search_sources_page(
    session: Session,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[SourceRecord], int]:
    """Filter, count, and page discovery sources in the database.

    Listing every source and slicing in Python meant the whole registry was
    loaded to render one page.
    """

    statement = select(DiscoverySource)
    normalized = query.strip()
    if normalized:
        pattern = f"%{normalized}%"
        statement = statement.where(
            or_(
                DiscoverySource.id.ilike(pattern),
                DiscoverySource.provider.ilike(pattern),
                DiscoverySource.title.ilike(pattern),
                DiscoverySource.description.ilike(pattern),
                cast(DiscoverySource.tags, Text).ilike(pattern),
            )
        )
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = session.scalars(
        statement.order_by(func.lower(DiscoverySource.title), DiscoverySource.id)
        .offset(max(offset, 0))
        .limit(max(limit, 1))
    ).all()
    return [to_source_record(row) for row in rows], total


def credential_schemes(session: Session, source_ids: list[str]) -> dict[tuple[str, str], str]:
    """Return the stored scheme for each (source_id, security_name) pair."""

    if not source_ids:
        return {}
    rows = session.execute(
        select(
            DiscoverySourceCredential.source_id,
            DiscoverySourceCredential.security_name,
            DiscoverySourceCredential.scheme,
        ).where(DiscoverySourceCredential.source_id.in_(source_ids))
    ).all()
    return {(row[0], row[1]): row[2] for row in rows}


def dependent_counts(session: Session, source_ids: list[str]) -> dict[str, int]:
    """Return how many Things depend on each of the given sources."""

    if not source_ids:
        return {}
    rows = session.execute(
        select(Thing.origin_source_id, func.count())
        .where(Thing.origin_source_id.in_(source_ids))
        .group_by(Thing.origin_source_id)
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}
