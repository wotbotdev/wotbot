from types import SimpleNamespace

import pytest
from langchain_core.embeddings import Embeddings

from wotbot.thing_indexer.prompting import (
    PROMPT_TEMPLATE,
    SUMMARY_PROMPT_VERSION,
    _extract_message_text,
)
from wotbot.search.vector_store import (
    SearchIndexDocument,
    SearchIndexMatch,
    SearchVectorStore,
)


class FakeEmbeddings(Embeddings):
    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        return [
            float(lowered.count("line")),
            float(lowered.count("temperature") + lowered.count("temp")),
            float(lowered.count("humidity")),
            float(len(lowered.split())),
        ]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_search_index_document_keeps_expected_fields():
    document = SearchIndexDocument(
        page_content="device summary",
        metadata={"title": "Alpha Sensor"},
    )

    assert document.page_content == "device summary"
    assert document.metadata["title"] == "Alpha Sensor"


def test_search_index_match_keeps_expected_fields():
    match = SearchIndexMatch(
        chunk_id="urn:thing:alpha",
        document="Temperature property",
        metadata={"title": "Alpha Sensor"},
        score=0.92,
    )

    assert match.chunk_id == "urn:thing:alpha"
    assert match.document == "Temperature property"
    assert match.metadata["title"] == "Alpha Sensor"
    assert match.score == 0.92


def test_extract_message_text_reads_string_payload():
    assert _extract_message_text("plain text") == "plain text"


def test_extract_message_text_reads_block_payload():
    content = [
        SimpleNamespace(text="First block"),
        SimpleNamespace(text="Second block"),
    ]

    assert _extract_message_text(content) == "First block\nSecond block"


def test_summary_prompt_does_not_assume_a_home_or_physical_installation():
    assert SUMMARY_PROMPT_VERSION == "v7"
    assert "do not assume every Thing has a physical installation" in PROMPT_TEMPLATE
    assert "kitchen" not in PROMPT_TEMPLATE.casefold()
    assert "home" not in PROMPT_TEMPLATE.casefold()


@pytest.mark.anyio
async def test_pgvector_search_vector_store_round_trip():
    store = SearchVectorStore(
        embeddings=FakeEmbeddings(),
        embedding_dimensions=4,
    )
    thing_id = "urn:thing:alpha"
    await store.replace_thing_chunks(
        thing_id,
        [
            (
                thing_id,
                SearchIndexDocument(
                    page_content="Line 3 process monitor with temperature summary",
                    metadata={"id": thing_id, "title": "Alpha"},
                ),
            ),
        ],
    )

    device_chunk = await store.get_device_chunk(thing_id)
    assert device_chunk is not None
    assert device_chunk.metadata["title"] == "Alpha"

    matches = await store.query_similar("line temperature", limit=3)
    assert matches
    assert matches[0].metadata["id"] == thing_id

    await store.delete_thing_chunks(thing_id)

    assert await store.get_device_chunk(thing_id) is None
    assert await store.query_similar("line temperature", limit=3) == []


@pytest.mark.anyio
async def test_pgvector_store_preserves_empty_list_metadata_values():
    store = SearchVectorStore(
        embeddings=FakeEmbeddings(),
        embedding_dimensions=4,
    )
    thing_id = "urn:thing:empty-meta"

    await store.replace_thing_chunks(
        thing_id,
        [
            (
                thing_id,
                SearchIndexDocument(
                    page_content="Device summary without tags",
                    metadata={
                        "id": thing_id,
                        "tags": [],
                        "locationCandidates": [],
                        "propertyNames": [],
                        "actionNames": [],
                        "eventNames": [],
                        "title": "No Tag Thing",
                    },
                ),
            )
        ],
    )

    stored = await store.get_device_chunk(thing_id)
    assert stored is not None
    assert stored.metadata["id"] == thing_id
    assert stored.metadata["title"] == "No Tag Thing"
    assert stored.metadata["tags"] == []
    assert stored.metadata["locationCandidates"] == []
