import asyncio

from wotbot.agent.tools.wot_registry import (
    REGISTRY_TOOLS,
    registry_health,
    things_search,
    things_validate,
)
from wotbot.search import set_active_search_service


def sample_thing(thing_id: str = "urn:thing:tool-test") -> dict[str, object]:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": "Tool Test Thing",
        "securityDefinitions": {
            "nosec_sc": {
                "scheme": "nosec",
            }
        },
        "security": "nosec_sc",
        "properties": {
            "temperature": {
                "type": "number",
                "forms": [{"href": "https://example.com/temperature"}],
            }
        },
    }


def test_registry_tools_include_catalog_and_runtime_tools():
    tool_names = {tool.name for tool in REGISTRY_TOOLS}

    assert "things_search" in tool_names
    assert "things_sparql" in tool_names
    assert "sparql_query" not in tool_names
    assert "query_knowledge" not in tool_names
    assert "registry_health" in tool_names
    assert "wot_read_property" in tool_names
    assert "wot_subscribe_event" in tool_names


def test_registry_health_tool_returns_in_process_registry_status():
    response = asyncio.run(registry_health.ainvoke({}))

    assert response["status"] == "ok"
    assert response["product"] == "wot_registry"


def test_things_validate_tool_returns_summary_counts():
    response = things_validate.invoke({"document": sample_thing()})

    assert response["id"] == "urn:thing:tool-test"
    assert response["property_count"] == 1
    assert response["action_count"] == 0


def test_things_search_uses_active_search_service():
    class FakeSearchService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def search(self, *, query: str, k: int):
            self.calls.append((query, k))
            return [{"id": "thing-a", "score": 0.9}]

    service = FakeSearchService()
    set_active_search_service(service)  # type: ignore[arg-type]
    try:
        response = asyncio.run(things_search.ainvoke({"query": " light ", "k": 2}))
    finally:
        set_active_search_service(None)

    assert response == {
        "items": [{"id": "thing-a", "score": 0.9}],
        "k": 2,
        "query": "light",
    }
    assert service.calls == [("light", 2)]


def test_things_search_rejects_out_of_range_k_before_searching():
    class FakeSearchService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def search(self, *, query: str, k: int):
            self.calls.append((query, k))
            return []

    service = FakeSearchService()
    set_active_search_service(service)  # type: ignore[arg-type]
    try:
        high = asyncio.run(things_search.ainvoke({"query": "temperature", "k": 50}))
        low = asyncio.run(things_search.ainvoke({"query": "temperature", "k": 0}))
    finally:
        set_active_search_service(None)

    assert "no operation ran" in high
    assert "no operation ran" in low
    assert service.calls == []


def test_things_search_returns_tool_error_for_empty_query():
    response = asyncio.run(things_search.ainvoke({"query": "   ", "k": 5}))

    assert response == {"error": "query must not be empty", "items": [], "query": ""}
