"""Application-owned A2A state; existing graph checkpoints remain authoritative."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base

# The states that reserve a context. Kept as SQL text for the partial unique
# index below, which is what actually enforces one active task per context.
RESERVED_STATES_SQL = (
    "'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', "
    "'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED'"
)


class A2ATaskRecord(Base):
    """One A2A task.

    ``context_id`` is the conversation's thread ID: A2A contexts are ordinary
    hidden threads (``kind='a2a'``), the way jobs attach to threads through
    ``Thread.job_id``. There is no separate context table.
    """

    __tablename__ = "a2a_tasks"
    __table_args__ = (
        Index("ix_a2a_tasks_owner_updated", "owner", "updated_at", "id"),
        # "One executing or paused task per context" as a database constraint,
        # rather than a mutable column three code paths have to keep in sync.
        Index(
            "uq_a2a_tasks_active_context",
            "context_id",
            unique=True,
            postgresql_where=text(f"state IN ({RESERVED_STATES_SQL})"),
        ),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    pending: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class A2AMessageRecord(Base):
    """Owner-wide retry identity, including messages that resume other tasks."""

    __tablename__ = "a2a_messages"
    owner: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("a2a_tasks.id", ondelete="CASCADE"), nullable=False
    )


class A2AArtifactRecord(Base):
    """Metadata only.

    Bytes stay where they were produced: exported files in the code executor's
    artifact store, panel markup in ``panel_versions``. This row exists to bind
    an artifact to its owning API key and to record when the link expires.
    """

    __tablename__ = "a2a_artifacts"
    __table_args__ = (
        Index(
            "idx_a2a_artifacts_owner_media_created_id", "owner", "media_type", "created_at", "id"
        ),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Soft task provenance: saved panels outlive task retention.
    task_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # The code executor's identifier, streamed on demand. Null for panels.
    executor_artifact_id: Mapped[str | None] = mapped_column(Text)
    # Only populated by the upgrade from the original A2A storage. New exports
    # remain in the executor. Deferred so listings never load old file bytes.
    legacy_content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    panel_version_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("panel_versions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
