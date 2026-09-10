"""Smoke the provider paths against real sources, one provider per case.

The unit suite mocks every byte of network I/O, which is right for pinning
parsing but says nothing about whether a provider still works against the
service it targets. These cases run register -> search -> discover -> onboard
for real and assert the shape of the Thing that comes out.

Opt in, because they contact third parties and depend on their uptime:

    RUN_EXTERNAL_DISCOVERY_TESTS=1 uv run pytest -q \\
        tests/integration/test_provider_smoke_live.py

A failure here is as likely to be a portal outage as a regression, so each case
reports the evidence trail the discovery service already collects. Use
``scripts/probe_sources.py`` to tell the two apart before chasing code.

Two providers cannot be smoked from the public internet and are covered by
``test_toolhive_and_edc_need_local_fixtures`` instead: ToolHive needs a running
ToolHive daemon, and EDC needs a dataspace connector plus a management API key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text

from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.discovery.service import DiscoveryService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("RUN_EXTERNAL_DISCOVERY_TESTS") != "1",
        reason="set RUN_EXTERNAL_DISCOVERY_TESTS=1 to run the live provider smoke tests",
    ),
]


@dataclass(frozen=True)
class Case:
    """One provider, one real source, and what its Thing must look like."""

    provider: str
    url: str
    queries: tuple[str, ...]
    #: Detection cannot reach every source. A DCAT catalog is a document, not a
    #: portal homepage, so it is registered by provider and config instead.
    detect: bool = True
    expect_download_actions: bool = False
    expect_http_actions: bool = False

    @property
    def id(self) -> str:
        return self.provider


CASES = [
    Case(
        provider="openapi",
        url="https://petstore3.swagger.io/api/v3/openapi.json",
        queries=("pet", ""),
        expect_http_actions=True,
    ),
    Case(
        provider="udata",
        url="https://www.data.gouv.fr/",
        queries=("GTFS", "transport", "mobility", ""),
        expect_download_actions=True,
    ),
    Case(
        provider="dcat",
        url="https://opendata.swiss/catalog.rdf",
        queries=("",),
        detect=False,
        expect_download_actions=True,
    ),
]


def _truncate() -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE things, discovery_source_credentials, discovery_sources, "
                "thing_event_outbox CASCADE"
            )
        )
        session.commit()


async def _register(service: DiscoveryService, case: Case) -> dict[str, Any]:
    if case.detect:
        registered = await service.register_source_url(source=case.url)
        assert registered.get("unsupported_source") is not True, (
            f"{case.provider}: detection refused {case.url}. Evidence: "
            + "; ".join(registered.get("probe_evidence", []))
        )
        return registered["source"]
    registered = await service.register_source(
        provider=case.provider,
        title=f"{case.provider} smoke source",
        description="",
        tags=[],
        config={"url": case.url},
        security=None,
        network_access="public",
    )
    return registered["source"]


async def _first_candidates(
    service: DiscoveryService, source_id: str, case: Case, thread_id: str
) -> list[dict[str, Any]]:
    for query in case.queries:
        response = await service.discover(
            source_id=source_id, query=query, limit=10, thread_id=thread_id
        )
        assert response.get("status") != "source_unavailable", (
            f"{case.provider}: source unavailable -- {response.get('message')}"
        )
        if response.get("items"):
            return list(response["items"])
    return []


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_provider_registers_searches_and_onboards(case: Case, jobs_integration_environment) -> None:
    async def smoke() -> None:
        service = DiscoveryService(Settings())
        thread_id = f"{case.provider}-smoke"

        source = await _register(service, case)
        assert source["provider"] == case.provider, (
            f"expected {case.provider}, detection produced {source['provider']}"
        )

        listed = await service.search_sources(query="", limit=50)
        assert any(item["source_id"] == source["source_id"] for item in listed["items"]), (
            "registered source did not appear in the agent-facing source search"
        )

        candidates = await _first_candidates(service, source["source_id"], case, thread_id)
        assert candidates, f"{case.provider}: no candidates for any of {case.queries}"
        for candidate in candidates:
            assert candidate["provider"] == case.provider
            assert candidate["candidate_id"]
            # A candidate is shown to a model, so it must stay metadata-only.
            assert "config" not in candidate
            assert "payload" not in candidate

        failures: list[str] = []
        for candidate in candidates:
            try:
                onboarded = await service.onboard(
                    candidate_id=candidate["candidate_id"], thread_id=thread_id
                )
            except (KeyError, TypeError, ValueError) as exc:
                failures.append(f"{candidate['title'][:40]}: {exc}")
                continue
            if onboarded.get("status") == "source_unavailable":
                failures.append(f"{candidate['title'][:40]}: source unavailable")
                continue
            thing = onboarded["thing"]
            actions = thing["affordances"]["actions"]
            expects_actions = case.expect_download_actions or case.expect_http_actions
            if expects_actions and not actions["count"]:
                failures.append(f"{candidate['title'][:40]}: onboarded with no actions")
                continue
            assert thing["id"].startswith("urn:wotbot:external:"), thing["id"]
            return

        pytest.fail(f"{case.provider}: no candidate onboarded. " + "; ".join(failures[:5]))

    _truncate()
    asyncio.run(smoke())


def test_toolhive_and_dataspace_providers_need_local_fixtures() -> None:
    """Record why three providers are absent above rather than silently uncovered.

    ToolHive needs a running daemon exposing /api/v1beta/workloads. EDC needs a
    connector plus a management API key, while tx-bootstrap needs a participant
    gateway with a populated federated catalog, so they cannot use a stable
    generic public fixture. All three are covered by unit tests.

    For EDC, tx-bootstrap provides one. After `up.sh` and `bootstrap.sh`,
    register a private source with the provider's DID in `counter_party_id` --
    a Tractus-X connector rejects a request that does not name it::

        {
          "provider": "edc-v3",
          "network_access": "private",
          "config": {
            "management_url": "http://consumer-controlplane:8081/management",
            "counter_party_address": "<provider DSP endpoint>",
            "counter_party_id": "<provider DID>"
          },
          "security": {"scheme": "apikey"}
        }

    then store the connector's X-Api-Key as the source credential. The request
    bodies and response fields this provider expects were taken from that
    repository's `deploy/local_compose/scripts/e2e-runtime.sh`.
    """

    from wotbot.discovery.providers import PROVIDERS

    smoked = {case.provider for case in CASES}
    unsmoked = set(PROVIDERS) - smoked
    assert unsmoked == {"toolhive", "edc-v3", "tx-bootstrap"}, (
        f"a provider is neither smoked nor documented as needing a local fixture: {unsmoked}"
    )
