from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base
from wotbot.workspaces import WorkspaceScoped


class DiscoverySource(WorkspaceScoped, Base):
    __tablename__ = "discovery_sources"
    __table_args__ = (
        # Each workspace may register the same external catalog once.
        UniqueConstraint(
            "workspace_id", "provider", "external_id", name="uq_discovery_source_identity"
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    network_access: Mapped[str] = mapped_column(Text, nullable=False, default="public")
    security_name: Mapped[str] = mapped_column(Text, nullable=False, default="source_sc")
    security_scheme: Mapped[str] = mapped_column(Text, nullable=False, default="nosec")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DiscoverySourceCredential(WorkspaceScoped, Base):
    __tablename__ = "discovery_source_credentials"
    __table_args__ = (UniqueConstraint("source_id", "security_name", name="uq_source_security"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("discovery_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    security_name: Mapped[str] = mapped_column(Text, nullable=False)
    scheme: Mapped[str] = mapped_column(Text, nullable=False)
    credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


@dataclass(frozen=True)
class SourceRecord:
    id: str
    provider: str
    external_id: str
    title: str
    description: str
    tags: list[str]
    config: dict[str, Any]
    network_access: str
    security_name: str
    security_scheme: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
