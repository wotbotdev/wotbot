from wotbot.thing_indexer.metadata import build_index_metadata


def test_build_index_metadata_shapes_vector_metadata():
    td_metadata = {
        "id": "urn:thing:alpha",
        "title": "Alpha Sensor",
        "description": "Process temperature monitor for assembly line 3",
        "tags": ["line-3", "sensor"],
        "propertyNames": ["temperature"],
        "actionNames": ["toggle"],
        "eventNames": ["overheated"],
    }

    metadata = build_index_metadata(
        td_metadata,
        event_type="update",
        td_hash="abc123",
        prompt_version="v-cleanup",
        summary_source="llm",
        summary_model="gpt-test",
        indexed_at="2026-03-17T10:00:00+00:00",
    )

    assert metadata == {
        "id": "urn:thing:alpha",
        "title": "Alpha Sensor",
        "description": "Process temperature monitor for assembly line 3",
        "tags": ["line-3", "sensor"],
        "locationCandidates": [],
        "propertyNames": ["temperature"],
        "actionNames": ["toggle"],
        "eventNames": ["overheated"],
        "eventType": "update",
        "tdHash": "abc123",
        "indexedAt": "2026-03-17T10:00:00+00:00",
        "promptVersion": "v-cleanup",
        "summarySource": "llm",
        "summaryModel": "gpt-test",
    }
