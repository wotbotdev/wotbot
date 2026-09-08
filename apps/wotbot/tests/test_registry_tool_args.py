import asyncio
import unittest
from unittest.mock import patch

from wotbot.agent.tools.wot_registry import REGISTRY_TOOLS, things_search, things_sparql
from wotbot.search import set_active_search_service


class RegistryToolArgsTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_search_service(None)

    def test_registry_tools_include_things_sparql(self) -> None:
        tool_names = {tool.name for tool in REGISTRY_TOOLS}
        self.assertIn("things_sparql", tool_names)
        self.assertNotIn("query_knowledge", tool_names)
        self.assertNotIn("sparql_query", tool_names)
        schema = things_sparql.args_schema.model_json_schema()
        self.assertIn("query", schema["properties"])
        self.assertIn("limit", schema["properties"])
        self.assertNotIn("intent", schema["properties"])
        self.assertNotIn("endpoint_id", schema["properties"])

    def test_things_search_rejects_out_of_range_k_before_searching(self) -> None:
        class FakeSearchService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            async def search(self, *, query: str, k: int):
                self.calls.append((query, k))
                return []

        service = FakeSearchService()
        set_active_search_service(service)  # type: ignore[arg-type]

        high = asyncio.run(things_search.ainvoke({"query": "temperature", "k": 50}))
        low = asyncio.run(things_search.ainvoke({"query": "temperature", "k": 0}))

        self.assertIn("no operation ran", high)
        self.assertIn("no operation ran", low)
        self.assertEqual(service.calls, [])

    def test_things_search_returns_tool_error_for_empty_query(self) -> None:
        response = asyncio.run(things_search.ainvoke({"query": "   ", "k": 5}))

        self.assertEqual(
            response,
            {"error": "query must not be empty", "items": [], "query": ""},
        )

    def test_things_sparql_runs_local_query_at_limit_boundary(self) -> None:
        calls = []

        class FakeRdfClient:
            async def query(self, *, query, limit):
                calls.append({"query": query, "limit": limit})
                return {"type": "select", "variables": ["s"], "rows": [], "truncated": False}

        with patch(
            "wotbot.agent.tools.wot_registry._rdf_client",
            return_value=FakeRdfClient(),
        ):
            response = asyncio.run(
                things_sparql.ainvoke({"query": "  SELECT * WHERE { ?s ?p ?o }  ", "limit": 500})
            )

        self.assertEqual(response["type"], "select")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["query"], "SELECT * WHERE { ?s ?p ?o }")
        self.assertEqual(calls[0]["limit"], 500)

    def test_things_sparql_returns_tool_error_for_empty_query(self) -> None:
        response = asyncio.run(things_sparql.ainvoke({"query": "   ", "limit": 5}))

        self.assertEqual(response, {"error": "query must not be empty", "query": ""})

    def test_things_sparql_returns_failure_on_client_error(self) -> None:
        class FailingRdfClient:
            async def query(self, *, query, limit):
                raise ValueError("rdf service down")

        with patch(
            "wotbot.agent.tools.wot_registry._rdf_client",
            return_value=FailingRdfClient(),
        ):
            response = asyncio.run(
                things_sparql.ainvoke({"query": "SELECT * WHERE { ?s ?p ?o }", "limit": 5})
            )

        self.assertEqual(
            response,
            {
                "status": "failed",
                "error": "rdf service down",
                "query": "SELECT * WHERE { ?s ?p ?o }",
                "limit": 5,
                "result": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
