from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wotbot.core.orm import Base
from wotbot.workspaces import WorkspaceScoped


class SearchIndexChunk(WorkspaceScoped, Base):
    __tablename__ = "search_index_chunks"
    __table_args__ = (
        Index("idx_search_index_chunks_thing_id", "thing_id"),
        Index(
            "idx_search_index_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thing_id: Mapped[str] = mapped_column(Text, nullable=False)
    page_content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    embedding: Mapped[Any] = mapped_column(VECTOR(), nullable=False)
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
