from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base

PanelCapabilities = list[dict[str, Any]]


class Panel(Base):
    """A pinned generative WoT mini-interface.

    Stores the agent's raw body markup (re-wrapped at render time so the latest
    bridge/CSP applies) plus the capability allowlist the UI enforces. Decoupled
    from chats: ``source_thread_id`` is soft provenance only, never a foreign
    key, so deleting a conversation never removes a pinned panel.
    """

    __tablename__ = "panels"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[PanelCapabilities] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    data: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    source_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class PanelVersion(Base):
    """Immutable saved state for a panel."""

    __tablename__ = "panel_versions"
    __table_args__ = (
        UniqueConstraint("panel_id", "version", name="uq_panel_versions_panel_version"),
        Index("ix_panel_versions_panel_id_created_at", "panel_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    panel_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("panels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column("version", Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[PanelCapabilities] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    data: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class PanelData(Base):
    """Immutable JSON bytes; only references and metadata enter agent messages.

    Unpinned snapshots expire with their source export. Pinning retains them
    until no saved panel/version references them. They share panel access scopes.
    """

    __tablename__ = "panel_data"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, deferred=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PanelValidationReport(Base):
    """Bounded evidence; image bytes are stored separately from report/tool JSON."""

    __tablename__ = "panel_validation_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("threads.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    document_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    report: Mapped[dict] = mapped_column(JSONB, nullable=False)
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    narrow_screenshot: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
