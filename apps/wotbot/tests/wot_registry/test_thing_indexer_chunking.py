from wotbot.thing_indexer.chunking import (
    build_chunk_content,
    generate_chunk,
)


def _make_td_metadata(**overrides):
    base = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "description": "Process temperature monitor for assembly line 3",
        "tags": ["line-3", "sensor"],
        "propertyNames": ["temperature"],
        "actionNames": [],
        "eventNames": [],
    }
    base.update(overrides)
    return base


def test_build_chunk_content_includes_summary():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
    }
    content = build_chunk_content(thing_td, "device summary")
    assert "device summary" in content


def test_build_chunk_content_includes_properties():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "properties": {
            "temperature": {
                "type": "number",
                "description": "Ambient temperature",
                "unit": "°C",
            },
            "humidity": {"type": "number", "description": "Relative humidity"},
        },
    }
    content = build_chunk_content(thing_td, "device summary")
    assert "Properties:" in content
    assert "- temperature (number, °C)" in content
    assert "- humidity (number)" in content


def test_build_chunk_content_includes_actions():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "actions": {
            "calibrate": {
                "description": "Calibrate the sensor",
                "input": {"type": "object", "properties": {"offset": {}}},
            },
        },
    }
    content = build_chunk_content(thing_td, "device summary")
    assert "Actions:" in content
    assert "- calibrate" in content
    assert "input: object [offset]" in content


def test_build_chunk_content_includes_events():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "events": {
            "overheated": {"description": "Sensor overheated"},
        },
    }
    content = build_chunk_content(thing_td, "device summary")
    assert "Events:" in content
    assert "- overheated" in content


def test_build_chunk_content_includes_enriched_semantic_types():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "@type": ["saref:TemperatureSensor", "http://www.w3.org/ns/sosa/Sensor"],
        "properties": {
            "temperature": {
                "type": "number",
                "unit": "°C",
                "@type": "saref:Temperature",
            },
        },
    }
    content = build_chunk_content(thing_td, "device summary")

    # Thing-level semantic classes become searchable text (local names, prefixes stripped).
    assert "Semantic types: TemperatureSensor, Sensor" in content
    # Affordance-level semantic type rides alongside the JSON type/unit.
    assert "- temperature (number, °C) [Temperature]" in content


def test_build_chunk_content_without_semantic_types_is_unchanged():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "properties": {"humidity": {"type": "number"}},
    }
    content = build_chunk_content(thing_td, "device summary")
    assert "Semantic types:" not in content
    assert "- humidity (number)" in content


def test_generate_chunk_returns_single_entry():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Line 3 Temperature Monitor",
        "properties": {
            "temperature": {"type": "number", "description": "Ambient temperature"},
            "humidity": {"type": "number", "description": "Relative humidity"},
        },
        "actions": {
            "calibrate": {"description": "Calibrate the sensor"},
        },
    }
    td_meta = _make_td_metadata(
        propertyNames=["humidity", "temperature"],
        actionNames=["calibrate"],
    )
    metadata = {"id": "urn:thing:alpha", "tdHash": "abc"}

    chunk_id, document = generate_chunk(thing_td, td_meta, "device summary", metadata)

    assert chunk_id == "urn:thing:alpha"
    assert "device summary" in document.page_content
    assert "- temperature" in document.page_content
    assert "- humidity" in document.page_content
    assert "- calibrate" in document.page_content


def test_generate_chunk_empty_affordances():
    thing_td = {
        "id": "urn:thing:alpha",
        "title": "Sensor",
    }
    td_meta = _make_td_metadata(propertyNames=[], actionNames=[], eventNames=[])
    metadata = {"id": "urn:thing:alpha"}

    chunk_id, document = generate_chunk(thing_td, td_meta, "summary", metadata)

    assert chunk_id == "urn:thing:alpha"
    assert document.page_content == "summary"
