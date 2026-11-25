from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from wotbot.core.database import get_session_factory
from wotbot.search.models import SearchIndexChunk
from wotbot.workspaces import current_workspace_id


@dataclass(frozen=True)
class SearchIndexDocument:
    page_content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SearchIndexMatch:
    chunk_id: str
    document: str
    metadata: dict[str, Any]
    score: float


def _coerce_metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _distance_to_score(value: Any) -> float:
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(distance):
        return 0.0
    return max(0.0, min(1.0, 1.0 - distance))


class SearchVectorStore:
    def __init__(
        self,
        *,
        embeddings: Embeddings | None,
        embedding_dimensions: int,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        if embedding_dimensions < 1:
            raise ValueError("embedding_dimensions must be a positive integer")

        self._embeddings = embeddings
        self._embedding_dimensions = embedding_dimensions
        self._session_factory = session_factory or get_session_factory()

    async def validate_schema(self) -> None:
        await asyncio.to_thread(self._validate_schema_sync)

    async def query_similar(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[SearchIndexMatch]:
        if limit < 1:
            return []

        await self.validate_schema()
        embeddings = self._require_embeddings()
        query_embedding = self._validate_embedding(
            await embeddings.aembed_query(query),
        )
        return await asyncio.to_thread(
            self._query_similar_sync,
            query_embedding,
            limit,
        )

    async def get_device_chunk(self, thing_id: str) -> SearchIndexDocument | None:
        await self.validate_schema()
        return await asyncio.to_thread(self._get_device_chunk_sync, thing_id)

    async def replace_thing_chunks(
        self,
        thing_id: str,
        chunks: list[tuple[str, SearchIndexDocument]],
    ) -> None:
        await self.validate_schema()
        documents_by_id = dict(chunks)
        next_chunk_ids = list(documents_by_id)

        rows: list[dict[str, Any]] = []
        if documents_by_id:
            embeddings = self._require_embeddings()
            documents = list(documents_by_id.values())
            vectors = await embeddings.aembed_documents(
                [document.page_content for document in documents]
            )
            workspace_id = current_workspace_id()
            rows = [
                {
                    "chunk_id": chunk_id,
                    "thing_id": thing_id,
                    "workspace_id": workspace_id,
                    "page_content": document.page_content,
                    "metadata_json": dict(document.metadata),
                    "embedding": self._validate_embedding(vector),
                }
                for chunk_id, document, vector in zip(
                    next_chunk_ids,
                    documents,
                    vectors,
                    strict=True,
                )
            ]

        await asyncio.to_thread(
            self._replace_thing_chunks_sync,
            thing_id,
            next_chunk_ids,
            rows,
        )

    async def delete_thing_chunks(self, thing_id: str) -> None:
        await self.validate_schema()
        await asyncio.to_thread(self._delete_thing_chunks_sync, thing_id)

    def _validate_schema_sync(self) -> None:
        with self._session_factory() as session:
            table_name = session.execute(text("SELECT to_regclass('search_index_chunks')")).scalar()
            if table_name is None:
                raise RuntimeError(
                    "search_index_chunks table is missing. Run init_db() or "
                    "`alembic upgrade head` before using semantic search."
                )

            embedding_type = session.execute(
                text(
                    """
                    SELECT format_type(attribute.atttypid, attribute.atttypmod)
                    FROM pg_attribute AS attribute
                    WHERE attribute.attrelid = to_regclass('search_index_chunks')
                        AND attribute.attname = 'embedding'
                        AND NOT attribute.attisdropped
                    """
                )
            ).scalar()
            expected_type = f"vector({self._embedding_dimensions})"
            if embedding_type != expected_type:
                raise RuntimeError(
                    "search_index_chunks.embedding has type "
                    f"{embedding_type or '<missing>'}, expected {expected_type}. "
                    "Run the database migrations with the configured "
                    "SEARCH_VECTOR_DIMENSIONS value."
                )

    def _query_similar_sync(
        self,
        query_embedding: list[float],
        limit: int,
    ) -> list[SearchIndexMatch]:
        distance = SearchIndexChunk.embedding.cosine_distance(query_embedding).label("distance")
        stmt = (
            select(SearchIndexChunk, distance)
            .order_by(distance, SearchIndexChunk.chunk_id)
            .limit(limit)
        )
        with self._session_factory() as session:
            rows = session.execute(stmt).all()

        return [
            SearchIndexMatch(
                chunk_id=chunk.chunk_id,
                document=chunk.page_content,
                metadata=_coerce_metadata(chunk.metadata_json),
                score=_distance_to_score(row_distance),
            )
            for chunk, row_distance in rows
        ]

    def _get_device_chunk_sync(self, thing_id: str) -> SearchIndexDocument | None:
        stmt = (
            select(SearchIndexChunk)
            .where(SearchIndexChunk.thing_id == thing_id)
            .order_by(SearchIndexChunk.chunk_id)
            .limit(1)
        )
        with self._session_factory() as session:
            chunk = session.scalar(stmt)

        if chunk is None:
            return None
        return SearchIndexDocument(
            page_content=chunk.page_content,
            metadata=_coerce_metadata(chunk.metadata_json),
        )

    def _replace_thing_chunks_sync(
        self,
        thing_id: str,
        next_chunk_ids: list[str],
        rows: list[dict[str, Any]],
    ) -> None:
        with self._session_factory() as session:
            if rows:
                stmt = insert(SearchIndexChunk).values(rows)
                session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[SearchIndexChunk.chunk_id],
                        set_={
                            "thing_id": stmt.excluded.thing_id,
                            "page_content": stmt.excluded.page_content,
                            "metadata_json": stmt.excluded.metadata_json,
                            "embedding": stmt.excluded.embedding,
                            "updated_at": func.now(),
                        },
                        # Never overwrite a chunk indexed for another workspace.
                        where=SearchIndexChunk.workspace_id == stmt.excluded.workspace_id,
                    )
                )

            stale_delete = delete(SearchIndexChunk).where(SearchIndexChunk.thing_id == thing_id)
            if next_chunk_ids:
                stale_delete = stale_delete.where(SearchIndexChunk.chunk_id.not_in(next_chunk_ids))
            session.execute(stale_delete)
            session.commit()

    def _delete_thing_chunks_sync(self, thing_id: str) -> None:
        with self._session_factory() as session:
            session.execute(delete(SearchIndexChunk).where(SearchIndexChunk.thing_id == thing_id))
            session.commit()

    def _validate_embedding(self, embedding: Sequence[float]) -> list[float]:
        values = [float(value) for value in embedding]
        if len(values) != self._embedding_dimensions:
            raise RuntimeError(
                "Embedding dimension mismatch: "
                f"SEARCH_VECTOR_DIMENSIONS is {self._embedding_dimensions}, "
                f"but the embedding provider returned {len(values)} values."
            )
        return values

    def _require_embeddings(self) -> Embeddings:
        if self._embeddings is None:
            raise RuntimeError(
                "OPENAI_EMBEDDING_API_KEY must be set for semantic search operations."
            )
        return self._embeddings
