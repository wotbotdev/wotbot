"""Federated discovery through a tx-bootstrap participant gateway.

The tx-bootstrap federated catalog is the source. Each cached entry resolves to
one remote EDC dataset and carries the counterparty address and identity needed
for a fresh offer request, negotiation, and transfer through the participant's
own management API.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlencode

from wotbot.discovery.errors import SourceProtocolError
from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import (
    CandidateDraft,
    DownloadRecord,
    OnboardingResult,
    ProviderConfigSpec,
    SearchIntent,
    SourceDefinition,
)
from wotbot.discovery.providers.base import (
    DiscoveryProvider,
    is_http_endpoint,
    source_json,
)
from wotbot.discovery.providers.edc_v3 import (
    _MAX_OPENAPI_BYTES,
    _OPENAPI_COMPILER_VERSION,
    _dataset_metadata,
    EdcApiDescription,
    EdcV3Provider,
    edc_api_description,
)
from wotbot.discovery.search import rank_candidates


@dataclass(frozen=True, slots=True)
class TxBootstrapEntry:
    entry_id: str
    dataset_id: str
    counter_party_address: str
    counter_party_id: str
    participant_name: str
    participant_bpn: str
    stale: bool
    dataset: dict[str, Any]


class TxBootstrapProvider(EdcV3Provider):
    """Discover all participants cached by one tx-bootstrap deployment."""

    name = "tx-bootstrap"
    public_max_requests = 128
    public_max_bytes = _MAX_OPENAPI_BYTES
    config = ProviderConfigSpec(
        fields=frozenset(
            {
                "participant_api_url",
                "protocol",
                "api_key_header",
                "poll_interval_seconds",
                "poll_timeout_seconds",
            }
        ),
        url_fields=("participant_api_url",),
        text_defaults=(
            ("protocol", "dataspace-protocol-http"),
            ("api_key_header", "X-Api-Key"),
        ),
        float_defaults=(("poll_interval_seconds", 2), ("poll_timeout_seconds", 120)),
        requires_secret=True,
        default_security_scheme="bearer",
        title="tx-bootstrap federated catalog",
    )

    def external_identity(self, config: dict[str, Any]) -> str:
        return str(config.get("participant_api_url") or "")

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        entries: dict[str, TxBootstrapEntry] = {}
        queries = intent.terms[:4] or ("",)
        page_limit = min(100, max(20, limit * 2))
        for query in queries:
            parameters: dict[str, str | int] = {"offset": 0, "limit": page_limit}
            if query:
                parameters["q"] = query
            payload = await source_json(
                "GET",
                f"{self._federated_catalog_url(source)}/v1/datasets?{urlencode(parameters)}",
                source=source,
                public_http=public_http,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise SourceProtocolError(
                    "tx-bootstrap federated catalog returned an invalid dataset page"
                )
            for value in payload["items"]:
                entry = self._parse_entry(value)
                entries.setdefault(entry.entry_id, entry)

        candidates: list[tuple[CandidateDraft, str]] = []
        for entry in entries.values():
            if not self._policy(entry.dataset):
                continue
            title, summary = _dataset_metadata(entry.dataset, fallback=entry.dataset_id)
            api_description = edc_api_description(entry.dataset)
            summary_metadata = api_description.summary or {}
            operation_terms = " ".join(
                f"{item['method']} {item['path']} {item.get('operationId', '')} "
                f"{item.get('title', '')} {item.get('description', '')}"
                for item in summary_metadata.get("operations", ())
                if isinstance(item, dict)
            )
            candidate = CandidateDraft(
                provider=self.name,
                source_id=source.id,
                external_id=entry.entry_id,
                kind="dataspace-api" if api_description.summary else "dataspace-asset",
                title=title,
                summary=summary,
                payload={
                    "spec_digest": api_description.fingerprint,
                    "compiler_version": _OPENAPI_COMPILER_VERSION,
                    "dataset_id": entry.dataset_id,
                    "participant_bpn": entry.participant_bpn,
                    "stale": entry.stale,
                },
            )
            search_text = " ".join(
                (
                    entry.dataset_id,
                    entry.participant_name,
                    entry.participant_bpn,
                    title,
                    summary,
                    str(summary_metadata.get("title") or ""),
                    str(summary_metadata.get("description") or ""),
                    operation_terms,
                )
            )
            candidates.append((candidate, search_text))
        return rank_candidates(intent, candidates, limit=limit, require_match=True)

    async def _selected_dataset(
        self,
        source: SourceDefinition,
        external_id: str,
        *,
        public_http: BoundedHttpClient | None,
    ) -> dict[str, Any]:
        # EdcV3Provider.acquire calls this method again after the federated
        # entry has already been resolved into a concrete target source.
        if source.get("management_url") and source.get("counter_party_address"):
            return await EdcV3Provider._selected_dataset(
                self,
                source,
                external_id,
                public_http=public_http,
            )
        entry = await self._entry(source, external_id, public_http=public_http)
        target = self._target_source(source, entry)
        return await EdcV3Provider._selected_dataset(
            self,
            target,
            entry.dataset_id,
            public_http=public_http,
        )

    async def acquire(
        self,
        source: SourceDefinition,
        *,
        external_id: str,
        title: str,
        resource_id: str | None,
        public_http: BoundedHttpClient | None = None,
    ) -> tuple[DownloadRecord, int | None]:
        entry = await self._entry(source, external_id, public_http=public_http)
        target = self._target_source(source, entry)
        return await EdcV3Provider.acquire(
            self,
            target,
            external_id=entry.dataset_id,
            title=title,
            resource_id=resource_id,
            public_http=public_http,
        )

    def _contract_request(
        self,
        source: SourceDefinition,
        dataset: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        request = EdcV3Provider._contract_request(self, source, dataset, policy)
        negotiation_policy = deepcopy(policy)
        negotiation_policy.setdefault("@context", "http://www.w3.org/ns/odrl.jsonld")

        dataset_id = str(dataset.get("@id") or dataset.get("id") or "").strip()
        if not dataset_id:
            raise SourceProtocolError("tx-bootstrap selected dataset has no asset id")
        if (
            negotiation_policy.get("target") is None
            and negotiation_policy.get("odrl:target") is None
        ):
            negotiation_policy["odrl:target"] = {"@id": dataset_id}

        assigner = str(source.get("counter_party_bpn") or "").strip()
        if not assigner:
            raise SourceProtocolError("tx-bootstrap federated entry has no participant BPN")
        if (
            negotiation_policy.get("assigner") is None
            and negotiation_policy.get("odrl:assigner") is None
        ):
            negotiation_policy["odrl:assigner"] = {"@id": assigner}
        return {**request, "policy": negotiation_policy}

    def _onboarding_result(
        self,
        candidate: CandidateDraft,
        api_description: EdcApiDescription,
    ) -> OnboardingResult:
        summary = deepcopy(api_description.summary)
        if summary is not None:
            summary["wotbot:generatedBy"] = self.name
        return EdcV3Provider._onboarding_result(
            self,
            candidate,
            replace(api_description, summary=summary),
        )

    def merge_refresh(
        self,
        current_document: dict[str, Any],
        generated_document: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        return DiscoveryProvider.merge_refresh(self, current_document, generated_document)

    def refresh_diff(
        self,
        current_document: dict[str, Any],
        replacement: dict[str, Any],
    ) -> dict[str, Any]:
        return DiscoveryProvider.refresh_diff(self, current_document, replacement)

    async def _entry(
        self,
        source: SourceDefinition,
        entry_id: str,
        *,
        public_http: BoundedHttpClient | None,
    ) -> TxBootstrapEntry:
        if not entry_id:
            raise SourceProtocolError("Thing has no tx-bootstrap federated catalog entry id")
        payload = await source_json(
            "GET",
            f"{self._federated_catalog_url(source)}/v1/datasets/{quote(entry_id, safe='')}",
            source=source,
            public_http=public_http,
        )
        return self._parse_entry(payload, expected_entry_id=entry_id)

    def _target_source(
        self,
        source: SourceDefinition,
        entry: TxBootstrapEntry,
    ) -> SourceDefinition:
        config = {
            **source.config,
            "management_url": f"{self._participant_api_url(source)}/api/management",
            "counter_party_address": entry.counter_party_address,
            "counter_party_id": entry.counter_party_id,
            "counter_party_bpn": entry.participant_bpn,
        }
        return replace(source, config=config)

    @staticmethod
    def _parse_entry(
        value: Any,
        *,
        expected_entry_id: str | None = None,
    ) -> TxBootstrapEntry:
        if not isinstance(value, dict):
            raise SourceProtocolError("tx-bootstrap returned an invalid federated catalog entry")
        entry_id = str(value.get("id") or "").strip()
        dataset_id = str(value.get("datasetId") or "").strip()
        counter_party_address = str(value.get("counterPartyAddress") or "").strip()
        counter_party_id = str(value.get("counterPartyId") or "").strip()
        participant = value.get("participant")
        dataset = value.get("dataset")
        if expected_entry_id is not None and entry_id != expected_entry_id:
            raise SourceProtocolError("tx-bootstrap returned a different federated catalog entry")
        if not entry_id or not dataset_id or not counter_party_id:
            raise SourceProtocolError("tx-bootstrap federated catalog entry is incomplete")
        if not is_http_endpoint(counter_party_address):
            raise SourceProtocolError("tx-bootstrap returned an invalid counterparty address")
        if not isinstance(participant, dict) or not isinstance(dataset, dict):
            raise SourceProtocolError("tx-bootstrap federated catalog entry is incomplete")
        return TxBootstrapEntry(
            entry_id=entry_id,
            dataset_id=dataset_id,
            counter_party_address=counter_party_address,
            counter_party_id=counter_party_id,
            participant_name=str(participant.get("name") or "").strip(),
            participant_bpn=str(participant.get("bpn") or "").strip(),
            stale=bool(value.get("stale")),
            dataset=dataset,
        )

    @staticmethod
    def _participant_api_url(source: SourceDefinition) -> str:
        return str(source.get("participant_api_url") or "").rstrip("/")

    def _federated_catalog_url(self, source: SourceDefinition) -> str:
        return f"{self._participant_api_url(source)}/api/federated-catalog"


__all__ = ["TxBootstrapEntry", "TxBootstrapProvider"]
