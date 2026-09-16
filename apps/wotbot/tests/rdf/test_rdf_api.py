from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from wotbot.rdf import api as rdf_api
from wotbot.rdf.models import RdfQueryRequest


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RaisingRdfStore:
    async def query(self, **_kwargs):
        raise RuntimeError("SyntaxError: Unknown prefix: wd")


@pytest.mark.anyio
async def test_query_rdf_returns_structured_error_for_query_failures() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(),
                rdf_store=RaisingRdfStore(),
            )
        )
    )
    payload = RdfQueryRequest(query="SELECT ?s WHERE { ?s ?p ?o }", limit=10)

    with (
        patch("wotbot.rdf.api.verify_internal_api_key"),
        pytest.raises(HTTPException) as exc_info,
    ):
        await rdf_api.query_rdf(request, payload)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "category": "syntax",
        "message": "SyntaxError: Unknown prefix: wd",
        "retryable": True,
    }
