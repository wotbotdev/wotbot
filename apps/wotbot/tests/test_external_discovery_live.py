import asyncio
import os

import pytest

from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import SourceDefinition
from wotbot.discovery.providers import resolve_public_source
from wotbot.discovery.providers.udata import UdataProvider
from wotbot.discovery.search import prepare_search_intent


@pytest.mark.external
@pytest.mark.skipif(
    os.environ.get("RUN_EXTERNAL_DISCOVERY_TESTS") != "1",
    reason="set RUN_EXTERNAL_DISCOVERY_TESTS=1 to probe the public portal",
)
def test_data_public_lu_is_discovered_without_catalog_side_effects() -> None:
    source, evidence, supported = asyncio.run(resolve_public_source("https://data.public.lu/en/"))

    assert supported
    assert source is not None
    assert source.provider == "udata"
    assert source.external_id == "https://data.public.lu"
    assert evidence


@pytest.mark.external
@pytest.mark.skipif(
    os.environ.get("RUN_EXTERNAL_DISCOVERY_TESTS") != "1",
    reason="set RUN_EXTERNAL_DISCOVERY_TESTS=1 to probe the public portal",
)
@pytest.mark.parametrize(
    "query",
    [
        "",
        "electricity",
        "electricity energy power electricity consumption production grid tariffs meters",
    ],
)
def test_data_public_lu_search_handles_large_resource_histories(query: str) -> None:
    source = SourceDefinition(
        id="https://data.public.lu",
        external_id="https://data.public.lu",
        provider="udata",
        title="Luxembourg open data",
        config={"url": "https://data.public.lu/en/"},
    )
    provider = UdataProvider()
    candidates = asyncio.run(
        provider.search(
            source,
            prepare_search_intent(query, source),
            25,
            public_http=BoundedHttpClient(
                max_bytes=provider.public_max_bytes,
                max_requests=provider.public_max_requests,
            ),
        )
    )

    assert candidates
    assert len({item.external_id for item in candidates}) == len(candidates)
    if query:
        assert any("electricity" in item.title.casefold() for item in candidates)
