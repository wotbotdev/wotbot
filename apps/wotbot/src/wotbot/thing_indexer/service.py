from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from wotbot.core.config import Settings
from wotbot.search.embeddings import (
    create_openai_client,
    create_openai_embeddings,
)
from wotbot.search.vector_store import SearchVectorStore
from wotbot.thing_indexer.chunking import generate_chunk
from wotbot.thing_indexer.metadata import build_index_metadata
from wotbot.thing_indexer.prompting import (
    SUMMARY_PROMPT_VERSION,
    generate_summary,
)
from wotbot.thing_indexer.summary_utils import (
    ThingTDMetadata,
    clean_text,
    compute_td_hash,
    extract_td_metadata,
    normalize_thing_td_payload,
)
from wotbot.workspaces import DEFAULT_WORKSPACE_ID, workspace_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedIndexEntry:
    thing_id: str
    chunk_id: str
    document: Any


class ThingIndexerService:
    def __init__(
        self,
        settings: Settings,
        *,
        vector_store: SearchVectorStore | None = None,
    ) -> None:
        self._settings = settings
        self._embeddings = create_openai_embeddings(
            base_url=settings.OPENAI_EMBEDDING_API_BASE_URL,
            api_key=settings.OPENAI_EMBEDDING_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
        )
        self._client = create_openai_client(
            base_url=settings.OPENAI_API_BASE_URL,
            api_key=settings.OPENAI_API_KEY,
        )
        self._vector_store = vector_store or SearchVectorStore(
            embeddings=self._embeddings,
            embedding_dimensions=settings.SEARCH_VECTOR_DIMENSIONS,
        )

    async def start(self) -> None:
        await self._validate_dependencies()

    async def close(self) -> None:
        await self._client.close()
        await self._embeddings.close()

    async def _validate_dependencies(self) -> None:
        if not self._settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY must be set for thing indexer embedding generation.")
        if not self._settings.OPENAI_MODEL:
            raise RuntimeError("OPENAI_MODEL must be set for thing indexer LLM summarization.")
        await self._vector_store.validate_schema()

    async def process_event(self, event: dict[str, Any]) -> None:
        thing_id = clean_text(event.get("id"))
        event_type = event.get("eventType")

        if not thing_id:
            logger.warning("Event missing 'id', cannot process.")
            return

        try:
            # Index into the workspace the Thing belongs to. Events published
            # before workspaces existed carry no workspace and stay in default.
            with workspace_scope(str(event.get("workspaceId") or DEFAULT_WORKSPACE_ID)):
                if event_type == "remove":
                    await self._remove_index_entry(thing_id)
                    return

                if event_type not in {"create", "update"}:
                    logger.info("Ignoring event type '%s' for thing '%s'", event_type, thing_id)
                    return

                prepared = await self._prepare_index_entry(
                    event,
                    thing_id=thing_id,
                    event_type=event_type,
                )
                await self._upsert_index_entry(prepared)
            logger.info("Successfully indexed thing: %s", thing_id)
        except Exception as exc:
            logger.error("Error managing vector for '%s': %s", thing_id, exc)
            raise

    async def _prepare_index_entry(
        self,
        event: dict[str, Any],
        *,
        thing_id: str,
        event_type: str,
    ) -> PreparedIndexEntry:
        thing_td = normalize_thing_td_payload(event)
        td_metadata: ThingTDMetadata = extract_td_metadata(thing_td)
        summary = await generate_summary(
            self._client,
            model=self._settings.OPENAI_MODEL,
            thing_td=thing_td,
        )
        metadata = build_index_metadata(
            td_metadata,
            event_type=event_type,
            td_hash=compute_td_hash(thing_td),
            prompt_version=SUMMARY_PROMPT_VERSION,
            summary_source="llm",
            summary_model=self._settings.OPENAI_MODEL,
        )
        chunk_id, document = generate_chunk(thing_td, td_metadata, summary.strip(), metadata)
        return PreparedIndexEntry(thing_id=thing_id, chunk_id=chunk_id, document=document)

    async def _upsert_index_entry(self, prepared: PreparedIndexEntry) -> None:
        await self._vector_store.replace_thing_chunks(
            prepared.thing_id,
            [(prepared.chunk_id, prepared.document)],
        )

    async def _remove_index_entry(self, thing_id: str) -> None:
        await self._vector_store.delete_thing_chunks(thing_id)
        logger.info("Removed vectors for deleted thing: %s", thing_id)
