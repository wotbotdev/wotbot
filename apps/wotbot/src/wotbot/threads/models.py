"""Thread metadata models."""

from __future__ import annotations

from enum import StrEnum
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base

DEFAULT_THREAD_TITLE = "New Chat"


class ThreadKind(StrEnum):
    CHAT = "chat"
    JOB = "job"
    A2A = "a2a"


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        CheckConstraint("kind IN ('chat', 'job', 'a2a')", name="ck_threads_kind"),
        Index("idx_threads_updated_at", "updated_at", "created_at"),
        Index("idx_threads_visible_kind", "kind", "visible", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_THREAD_TITLE)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=ThreadKind.CHAT.value,
    )
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    job_id: Mapped[str | None] = mapped_column(String)
    # Which API key owns this conversation. Set only for kind='a2a'.
    owner_api_key_id: Mapped[str | None] = mapped_column(String, index=True)
    # Older contexts had a separate public ID. Retain that alias without
    # renaming the thread used by checkpoints, panels and virtual Things.
    legacy_a2a_context_id: Mapped[str | None] = mapped_column(String, unique=True)


class ThreadRecord(TypedDict):
    id: str
    title: str
    createdAt: str
    updatedAt: str
    kind: str
    visible: bool
    jobId: str | None


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str | None = None
    title: str | None = None
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")


class UpdateThreadTitleRequest(BaseModel):
    title: str
    force: bool = False

    @field_validator("title")
    @classmethod
    def require_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Title is required")
        return title
