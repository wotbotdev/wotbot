from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from wotbot.discovery.errors import SourceProtocolError
from wotbot.discovery.models import SearchIntent, SourceDefinition
from wotbot.discovery.providers.tx_bootstrap import TxBootstrapProvider


def tx_source() -> SourceDefinition:
    return SourceDefinition(
        id="https://consumer.example",
        provider="tx-bootstrap",
        title="Consumer federated catalog",
        external_id="https://consumer.example",
        config={
            "participant_api_url": "https://consumer.example",
            "protocol": "dataspace-protocol-http",
            "poll_interval_seconds": 2,
            "poll_timeout_seconds": 120,
        },
        security_scheme="nosec",
    )


def dataset() -> dict[str, Any]:
    return {
        "@id": "asset-1",
        "dct:title": "Road network",
        "dct:description": "Current road geometry",
        "odrl:hasPolicy": {"@id": "offer-1"},
    }


def federated_entry() -> dict[str, Any]:
    return {
        "id": "entry-1",
        "datasetId": "asset-1",
        "counterPartyAddress": "https://provider.example/api/v1/dsp",
        "counterPartyId": "did:web:provider.example:BPNL000000000001",
        "participant": {
            "name": "Example Provider",
            "bpn": "BPNL000000000001",
        },
        "stale": False,
        "dataset": dataset(),
    }


class TxBootstrapDiscoveryTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_the_federated_entry_as_the_stable_identity(self) -> None:
        sender = AsyncMock(
            return_value={"items": [federated_entry()], "total": 1, "offset": 0, "limit": 20}
        )

        with patch("wotbot.discovery.providers.tx_bootstrap.source_json", new=sender):
            candidates = await TxBootstrapProvider().search(
                tx_source(),
                SearchIntent(original=""),
                10,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "tx-bootstrap")
        self.assertEqual(candidates[0].external_id, "entry-1")
        self.assertEqual(candidates[0].payload["dataset_id"], "asset-1")
        self.assertEqual(candidates[0].payload["participant_bpn"], "BPNL000000000001")
        request = sender.await_args
        self.assertEqual(request.args[0], "GET")
        self.assertEqual(
            request.args[1],
            "https://consumer.example/api/federated-catalog/v1/datasets?offset=0&limit=20",
        )

    async def test_onboarding_resolves_each_entry_to_its_own_counterparty(self) -> None:
        provider = TxBootstrapProvider()
        federated_sender = AsyncMock(
            side_effect=[{"items": [federated_entry()]}, federated_entry()]
        )
        edc_sender = AsyncMock(return_value=dataset())

        with (
            patch(
                "wotbot.discovery.providers.tx_bootstrap.source_json",
                new=federated_sender,
            ),
            patch("wotbot.discovery.providers.edc_v3.source_json", new=edc_sender),
        ):
            [candidate] = await provider.search(tx_source(), SearchIntent(original=""), 10)
            result = await provider.onboarding_document(
                tx_source(),
                candidate,
                runtime=AsyncMock(),
            )

        self.assertEqual(
            federated_sender.await_args.args[1],
            "https://consumer.example/api/federated-catalog/v1/datasets/entry-1",
        )
        direct_request = edc_sender.await_args
        self.assertEqual(
            direct_request.args[1],
            "https://consumer.example/api/management/v3/catalog/dataset/request",
        )
        self.assertEqual(direct_request.kwargs["body"]["@id"], "asset-1")
        self.assertEqual(
            direct_request.kwargs["body"]["counterPartyAddress"],
            "https://provider.example/api/v1/dsp",
        )
        self.assertEqual(
            direct_request.kwargs["body"]["counterPartyId"],
            "did:web:provider.example:BPNL000000000001",
        )
        self.assertEqual(result.document["wotbot:generation"]["provider"], "tx-bootstrap")
        self.assertGreater(candidate.payload["compiler_version"], 2)
        self.assertEqual(
            result.document["wotbot:generation"]["compilerVersion"],
            candidate.payload["compiler_version"],
        )
        self.assertEqual(
            result.document["actions"]["download_asset"]["wotbot:generatedBy"],
            "tx-bootstrap",
        )

    async def test_acquisition_enriches_the_offer_for_tractus_x_negotiation(self) -> None:
        offered = dataset()
        original_policy = dict(offered["odrl:hasPolicy"])
        federated_sender = AsyncMock(return_value=federated_entry())
        edc_sender = AsyncMock(
            side_effect=[
                offered,
                {"@id": "negotiation-1"},
                {"state": "FINALIZED", "contractAgreementId": "agreement-1"},
                {"@id": "transfer-1"},
                {"state": "STARTED"},
                {
                    "endpoint": "https://provider.example/data",
                    "authorization": "Bearer edr-token",
                },
            ]
        )

        with (
            patch(
                "wotbot.discovery.providers.tx_bootstrap.source_json",
                new=federated_sender,
            ),
            patch("wotbot.discovery.providers.edc_v3.source_json", new=edc_sender),
        ):
            download, _ttl = await TxBootstrapProvider().acquire(
                tx_source(),
                external_id="entry-1",
                title="Road network",
                resource_id=None,
            )

        negotiation = edc_sender.await_args_list[1]
        self.assertTrue(negotiation.args[1].endswith("/v3/contractnegotiations"))
        body = negotiation.kwargs["body"]
        self.assertEqual(body["counterPartyId"], "did:web:provider.example:BPNL000000000001")
        self.assertEqual(body["policy"]["@context"], "http://www.w3.org/ns/odrl.jsonld")
        self.assertEqual(body["policy"]["odrl:target"], {"@id": "asset-1"})
        self.assertEqual(
            body["policy"]["odrl:assigner"],
            {"@id": "BPNL000000000001"},
        )
        self.assertEqual(offered["odrl:hasPolicy"], original_policy)
        self.assertEqual(download.endpoint, "https://provider.example/data")
        self.assertEqual(download.headers, {"Authorization": "Bearer edr-token"})

    def test_contract_request_preserves_catalog_target_and_assigner(self) -> None:
        provider = TxBootstrapProvider()
        entry = provider._parse_entry(federated_entry())
        target = provider._target_source(tx_source(), entry)
        policy = {
            "@id": "offer-1",
            "odrl:target": {"@id": "catalog-asset"},
            "odrl:assigner": {"@id": "catalog-assigner"},
        }

        body = provider._contract_request(target, dataset(), policy)

        self.assertEqual(body["policy"]["odrl:target"], {"@id": "catalog-asset"})
        self.assertEqual(body["policy"]["odrl:assigner"], {"@id": "catalog-assigner"})

    def test_federated_entry_requires_dynamic_counterparty_coordinates(self) -> None:
        invalid = federated_entry()
        invalid.pop("counterPartyAddress")

        with self.assertRaisesRegex(SourceProtocolError, "invalid counterparty address"):
            TxBootstrapProvider._parse_entry(invalid)


if __name__ == "__main__":
    unittest.main()
