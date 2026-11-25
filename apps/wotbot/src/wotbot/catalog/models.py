from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base
from wotbot.workspaces import DEFAULT_WORKSPACE_ID, WorkspaceScoped

ThingDocument = dict[str, Any]


class ThingConflictError(RuntimeError):
    """Raised when a Thing document conflicts with an existing catalog record."""


class Thing(WorkspaceScoped, Base):
    __tablename__ = "things"
    __table_args__ = (
        CheckConstraint(
            "(origin_kind = 'manual' "
            "AND origin_provider IS NULL AND origin_external_id IS NULL "
            "AND origin_source_id IS NULL) OR "
            "(origin_kind = 'discovery' AND origin_provider IS NOT NULL "
            "AND origin_external_id IS NOT NULL AND origin_source_id IS NOT NULL)",
            name="ck_things_origin_shape",
        ),
        Index(
            "uq_things_discovery_resource_origin",
            "origin_source_id",
            "origin_provider",
            "origin_external_id",
            unique=True,
            postgresql_where=text("origin_kind = 'discovery'"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    origin_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_source_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("discovery_sources.id", ondelete="RESTRICT"),
        nullable=True,
    )
    document: Mapped[ThingDocument] = mapped_column(JSONB, nullable=False)
    document_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


@dataclass(frozen=True)
class ThingRecord:
    id: str
    title: str
    description: str
    tags: list[str]
    origin_kind: str
    origin_provider: str | None
    origin_external_id: str | None
    origin_source_id: str | None
    document: ThingDocument
    document_hash: str
    workspace_id: str = DEFAULT_WORKSPACE_ID
