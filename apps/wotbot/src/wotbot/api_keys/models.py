from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base
from wotbot.workspaces.context import DEFAULT_WORKSPACE_ID
from wotbot.workspaces.models import Workspace  # noqa: F401  (FK target must be mapped)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_key_hash", "key_hash"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Every request made with this key runs in this workspace. Deliberately not
    # WorkspaceScoped: keys are looked up before any workspace is known.
    workspace_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("workspaces.id", ondelete="RESTRICT", name="fk_api_keys_workspace_id"),
        nullable=False,
        default=DEFAULT_WORKSPACE_ID,
        server_default=DEFAULT_WORKSPACE_ID,
        index=True,
    )
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
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    key_prefix: str
    name: str
    scopes: list[str]
    user_id: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool = True
    workspace_id: str = DEFAULT_WORKSPACE_ID


@dataclass(frozen=True)
class ApiKeyRow:
    id: str
    key_prefix: str
    key_hash: str
    name: str
    scopes: list[str]
    user_id: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool = True
    workspace_id: str = DEFAULT_WORKSPACE_ID
