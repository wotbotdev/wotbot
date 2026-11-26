"""SPARQL queries limited to the Things of one workspace."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wotbot.rdf import api as rdf_api
from wotbot.rdf.iris import thing_graph_iri
from wotbot.rdf.models import RdfQueryRequest
from wotbot.rdf.store import RdfStoreService

ALPHA = "urn:thing:alpha"
BETA = "urn:thing:beta"
BETA_GRAPH = thing_graph_iri(BETA)
NAME = "http://schema.org/name"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _thing(thing_id: str, name: str) -> dict[str, object]:
    return {
        "@context": {"name": NAME},
        "@id": thing_id,
        "id": thing_id,
        "name": name,
    }


@pytest.fixture
async def store(tmp_path) -> RdfStoreService:
    rdf = RdfStoreService(str(tmp_path / "rdf"))
    await rdf.upsert_thing(ALPHA, _thing(ALPHA, "Alpha"))
    await rdf.upsert_thing(BETA, _thing(BETA, "Beta"))
    return rdf


def _names(response: dict) -> list[str]:
    return sorted(row["name"]["value"] for row in response["rows"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    [
        f"SELECT ?name WHERE {{ ?s <{NAME}> ?name }}",
        f"SELECT ?name WHERE {{ GRAPH ?g {{ ?s <{NAME}> ?name }} }}",
        f"SELECT ?name FROM <{BETA_GRAPH}> WHERE {{ ?s <{NAME}> ?name }}",
        f"SELECT ?name FROM NAMED <{BETA_GRAPH}> WHERE {{ GRAPH ?g {{ ?s <{NAME}> ?name }} }}",
        (
            f"SELECT ?name WHERE {{ {{ GRAPH <{BETA_GRAPH}> {{ ?s <{NAME}> ?name }} }}"
            f" UNION {{ ?s <{NAME}> ?name }} }}"
        ),
    ],
    ids=["default-graph", "graph-variable", "from", "from-named", "graph-iri"],
)
async def test_restricted_queries_cannot_reach_other_things(store, query) -> None:
    response = await store.query(query=query, limit=10, thing_ids=[ALPHA])
    assert _names(response) == ["Alpha"]


@pytest.mark.anyio
async def test_ask_and_construct_are_restricted_too(store) -> None:
    ask = await store.query(
        query=f"ASK {{ GRAPH <{BETA_GRAPH}> {{ ?s ?p ?o }} }}", limit=10, thing_ids=[ALPHA]
    )
    assert ask["boolean"] is False

    construct = await store.query(
        query="CONSTRUCT { ?s ?p ?o } WHERE { GRAPH ?g { ?s ?p ?o } }",
        limit=50,
        thing_ids=[ALPHA],
    )
    assert ALPHA in construct["rdf"]
    assert BETA not in construct["rdf"]


@pytest.mark.anyio
async def test_a_workspace_without_things_sees_nothing(store) -> None:
    response = await store.query(
        query=f"SELECT ?name WHERE {{ GRAPH ?g {{ ?s <{NAME}> ?name }} }}",
        limit=10,
        thing_ids=[],
    )
    assert response["rows"] == []


@pytest.mark.anyio
async def test_queries_without_a_thing_list_stay_unrestricted(store) -> None:
    response = await store.query(query=f"SELECT ?name WHERE {{ ?s <{NAME}> ?name }}", limit=10)
    assert _names(response) == ["Alpha", "Beta"]


@pytest.mark.anyio
async def test_the_http_endpoint_passes_the_thing_list_through(store) -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(), rdf_store=store))
    )
    payload = RdfQueryRequest(
        query=f"SELECT ?name WHERE {{ ?s <{NAME}> ?name }}", limit=10, thing_ids=[BETA]
    )
    with patch("wotbot.rdf.api.verify_internal_api_key"):
        response = await rdf_api.query_rdf(request, payload)  # type: ignore[arg-type]
    assert _names(response) == ["Beta"]
