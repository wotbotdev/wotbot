"""EDC v3: assets offered through an Eclipse Dataspace Connector.

Expects an EDC Management API v3 and speaks its catalog, single-dataset,
contract-negotiation, transfer-process, and endpoint-data-reference endpoints.

Configuration
    ``management_url``
        This deployment's own EDC management API. Required.
    ``counter_party_address``
        The partner connector's protocol endpoint. Required, and it is this
        source's canonical identity rather than ``management_url``.
    ``counter_party_id``
        The partner's DID or BPN. Optional for a plain EDC, which ignores it,
        but **required by Tractus-X connectors**, which reject a request that
        does not name the provider. Sent only when set.
    ``protocol``
        Dataspace protocol name. Defaults to ``dataspace-protocol-http``.
    ``api_key_header``
        Header carrying the management API key. Defaults to ``X-Api-Key``.
    ``poll_interval_seconds`` / ``poll_timeout_seconds``
        Negotiation and transfer polling. Default to 2 and 120.

A credential is mandatory: the management API is privileged, so this provider
declares ``requires_secret`` and defaults to the ``apikey`` scheme. It also
does not detect. A dataspace membership is arranged out of band, so a source is
registered explicitly and never inferred from a pasted URL.

Response parsing is deliberately tolerant, because the same connector family
answers in several shapes: a catalog may arrive as one object or a list of
them, a single dataset may be inlined rather than wrapped, and fields may be
compacted (``state``) or expanded (``edc:state``) depending on the
distribution. ``authKey`` is ambiguous in particular -- a header name in a
plain EDC, the token itself in Tractus-X -- so it is read as a token only when
no separate value field is present and it is not a bare header name.

Only datasets carrying an ``odrl:hasPolicy`` offer are shown, because a dataset
without one cannot be negotiated for. A tx-bootstrap ``txb:apiDescription`` is
compiled into at most 30 ordinary WoT actions using the same bounded OpenAPI
compiler as a direct OpenAPI source. Their forms target the local provider
binding: the contract is negotiated and the acquired API endpoint is called at
invocation, not at onboarding. Other assets receive a single download action.
Nothing is agreed to merely by browsing or onboarding.

Acquisition is the sequence negotiate, poll to FINALIZED or VERIFIED, request
transfer, poll to STARTED or COMPLETED, then read the endpoint data reference.
Two consequences matter. It is slow and bounded by ``poll_timeout_seconds``,
which is why the request budget is raised to 128 and why it should eventually
run as a job rather than inside a request. And the reference carries a
short-lived bearer secret, so its header name is validated against a denylist
and a token grammar before use, and its expiry becomes the download's TTL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

from wotbot.discovery.errors import (
    SourceProtocolError,
    SourceUnavailableError,
    StaleCandidateError,
)
from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import (
    CandidateDraft,
    DownloadRecord,
    OnboardingResult,
    ProviderConfigSpec,
    ProviderResponse,
    SearchIntent,
    SourceDefinition,
)
from wotbot.discovery.providers.base import (
    TD_CONTEXT,
    WOTBOT_CONTEXT,
    DiscoveryProvider,
    OnboardingRuntime,
    provider_action_href,
    provider_download_action,
    provider_thing_id,
    source_client,
    source_json,
    text,
)
from wotbot.discovery.providers.openapi import (
    OpenApiError,
    compile_provider_actions,
    parse_openapi,
)
from wotbot.discovery.search import rank_candidates

JSONLD_CONTEXT = {"@vocab": "https://w3id.org/edc/v0.0.1/ns/"}
EDC_PREFIX = "edc:"
FORBIDDEN_SECRET_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}
# Names a connector would use for the header itself. An ``authKey`` holding one
# of these and nothing else is a classic EDR missing its token, not a token.
AUTH_HEADER_NAMES = {"authorization", "x-api-key", "x-auth-token", "apikey"}
TXB_API_DESCRIPTION = "txb:apiDescription"
TXB_API_DESCRIPTION_URI = (
    "https://github.com/connected-intelligent-systems/tx-bootstrap/ns/apiDescription"
)
EDC_PROPERTIES_URI = "https://w3id.org/edc/v0.0.1/ns/properties"
_OPENAPI_COMPILER_VERSION = 3
_MAX_OPENAPI_BYTES = 4 * 1024 * 1024
_MAX_OPENAPI_OPERATIONS = 2_000
_MAX_SUMMARY_OPERATIONS = 30
_MAX_TD_BYTES = 512 * 1024
_MAX_API_RESPONSE_BYTES = 512 * 1024
_HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class EdcApiDescription:
    fingerprint: str
    summary: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    warning: str = ""


class EdcV3Provider(DiscoveryProvider):
    name = "edc-v3"
    capabilities = ("search", "onboard", "refresh")
    # Acquisition polls negotiation and transfer state, so EDC needs a larger
    # bounded request budget than ordinary catalog providers.
    public_max_requests = 128
    config = ProviderConfigSpec(
        fields=frozenset(
            {
                "management_url",
                "counter_party_address",
                "counter_party_id",
                "protocol",
                "api_key_header",
                "poll_interval_seconds",
                "poll_timeout_seconds",
            }
        ),
        url_fields=("management_url", "counter_party_address"),
        text_defaults=(
            ("protocol", "dataspace-protocol-http"),
            ("api_key_header", "X-Api-Key"),
            ("counter_party_id", ""),
        ),
        float_defaults=(("poll_interval_seconds", 2), ("poll_timeout_seconds", 120)),
        requires_secret=True,
        title="EDC v3 catalog",
    )

    def external_identity(self, config: dict[str, Any]) -> str:
        return str(config.get("counter_party_address") or "")

    def _party(self, source: SourceDefinition) -> dict[str, str]:
        """The counterparty fields every request to a connector carries.

        Tractus-X connectors reject a request that does not name the provider's
        DID or BPN, while a plain EDC ignores the field, so it is sent whenever
        the source has one and omitted otherwise.
        """

        party = {
            "counterPartyAddress": str(source.get("counter_party_address")),
            "protocol": str(source.get("protocol", "dataspace-protocol-http")),
        }
        identity = str(source.get("counter_party_id") or "").strip()
        if identity:
            party["counterPartyId"] = identity
        return party

    def _catalog_request(
        self,
        source: SourceDefinition,
        *,
        limit: int,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        filters: list[dict[str, str]] = []
        if external_id:
            filters.append({"operandLeft": "@id", "operator": "=", "operandRight": external_id})
        return {
            "@context": JSONLD_CONTEXT,
            "@type": "CatalogRequest",
            **self._party(source),
            "querySpec": {"offset": 0, "limit": limit, "filterExpression": filters},
        }

    async def _catalog(
        self,
        source: SourceDefinition,
        *,
        limit: int,
        external_id: str | None = None,
        public_http: BoundedHttpClient | None = None,
    ) -> list[dict[str, Any]]:
        catalog = await source_json(
            "POST",
            f"{source.get('management_url')}/v3/catalog/request",
            source=source,
            public_http=public_http,
            body=self._catalog_request(source, limit=limit, external_id=external_id),
        )
        return catalog_datasets(catalog)

    async def _selected_dataset(
        self,
        source: SourceDefinition,
        external_id: str,
        *,
        public_http: BoundedHttpClient | None,
    ) -> dict[str, Any]:
        payload = await source_json(
            "POST",
            f"{source.get('management_url')}/v3/catalog/dataset/request",
            source=source,
            public_http=public_http,
            body={
                "@context": JSONLD_CONTEXT,
                "@type": "DatasetRequest",
                "@id": external_id,
                **self._party(source),
            },
        )
        dataset = next(
            (
                item
                for item in catalog_datasets(payload)
                if str(item.get("@id") or item.get("id") or "").strip() == external_id
            ),
            None,
        )
        if dataset is None:
            raise SourceProtocolError("The selected EDC asset is no longer offered")
        return dataset

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        candidates: list[tuple[CandidateDraft, str]] = []
        for dataset in await self._catalog(
            source,
            limit=max(50, limit),
            public_http=public_http,
        ):
            external_id = str(dataset.get("@id") or dataset.get("id") or "").strip()
            title, summary = _dataset_metadata(dataset, fallback=external_id)
            if not external_id:
                continue
            if not self._policy(dataset):
                continue
            api_description = edc_api_description(dataset)
            candidate = CandidateDraft(
                provider=self.name,
                source_id=source.id,
                external_id=external_id,
                kind=("dataspace-api" if api_description.summary else "dataspace-asset"),
                title=title,
                summary=summary,
                payload={
                    "spec_digest": api_description.fingerprint,
                    "compiler_version": _OPENAPI_COMPILER_VERSION,
                },
            )
            summary_metadata = api_description.summary or {}
            operation_terms = " ".join(
                f"{item['method']} {item['path']} {item.get('operationId', '')} "
                f"{item.get('title', '')} {item.get('description', '')}"
                for item in summary_metadata.get("operations", ())
                if isinstance(item, dict)
            )
            api_terms = (
                f"{summary_metadata.get('title', '')} "
                f"{summary_metadata.get('description', '')} {operation_terms}"
            )
            candidates.append((candidate, f"{external_id} {title} {summary} {api_terms}"))
        return rank_candidates(intent, candidates, limit=limit, require_match=True)

    async def onboarding_document(
        self,
        source: SourceDefinition,
        candidate: CandidateDraft,
        *,
        runtime: OnboardingRuntime,
    ) -> OnboardingResult:
        del runtime
        dataset = await self._selected_dataset(
            source,
            candidate.external_id,
            public_http=self._public_client(source),
        )
        api_description = edc_api_description(dataset)
        expected_fingerprint = str(candidate.payload.get("spec_digest") or "")
        if expected_fingerprint != api_description.fingerprint:
            raise StaleCandidateError(
                "EDC asset API description changed; discover the source again"
            )
        title, summary = _dataset_metadata(dataset, fallback=candidate.external_id)
        current_candidate = CandidateDraft(
            provider=candidate.provider,
            source_id=candidate.source_id,
            external_id=candidate.external_id,
            kind="dataspace-api" if api_description.summary else "dataspace-asset",
            title=title,
            summary=summary,
            payload=candidate.payload,
        )
        return self._onboarding_result(current_candidate, api_description)

    async def refresh_document(
        self,
        source: SourceDefinition,
        current_document: dict[str, Any],
        *,
        runtime: OnboardingRuntime,
        external_id: str = "",
    ) -> OnboardingResult:
        del runtime
        if not external_id:
            raise SourceProtocolError("Thing has no EDC origin asset id")
        dataset = await self._selected_dataset(
            source,
            external_id,
            public_http=self._public_client(source),
        )
        api_description = edc_api_description(dataset)
        candidate = CandidateDraft(
            provider=self.name,
            source_id=source.id,
            external_id=external_id,
            kind="dataspace-api" if api_description.summary else "dataspace-asset",
            title=str(current_document.get("title") or external_id),
            summary=str(current_document.get("description") or ""),
        )
        return self._onboarding_result(candidate, api_description)

    def merge_refresh(
        self,
        current_document: dict[str, Any],
        generated_document: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Upgrade the original unmarked EDC download nodes during refresh."""

        return super().merge_refresh(
            _with_legacy_edc_markers(current_document),
            generated_document,
        )

    def refresh_diff(
        self,
        current_document: dict[str, Any],
        replacement: dict[str, Any],
    ) -> dict[str, Any]:
        return super().refresh_diff(
            _with_legacy_edc_markers(current_document),
            replacement,
        )

    def _onboarding_result(
        self,
        candidate: CandidateDraft,
        api_description: EdcApiDescription,
    ) -> OnboardingResult:
        thing_id = provider_thing_id(self.name, candidate.source_id, candidate.external_id)
        document: dict[str, Any] = {
            "@context": [TD_CONTEXT, WOTBOT_CONTEXT],
            "id": thing_id,
            "title": candidate.title,
            "description": candidate.summary,
            "security": ["nosec_sc"],
            "securityDefinitions": {
                "nosec_sc": {
                    "scheme": "nosec",
                    "wotbot:generatedBy": self.name,
                }
            },
            "wotbot:generation": {
                "provider": self.name,
                "compilerVersion": _OPENAPI_COMPILER_VERSION,
                "specificationDigest": api_description.fingerprint,
                "externalId": candidate.external_id,
            },
        }
        warnings: list[str] = []
        if api_description.warning:
            warnings.append(api_description.warning)
        if api_description.summary is not None and api_description.document is not None:
            document["wotbot:apiDescription"] = api_description.summary
            actions, operation_keys, compiler_warnings, operation_count = compile_provider_actions(
                api_description.document,
                thing_id=thing_id,
                provider=self.name,
                max_operations=_MAX_SUMMARY_OPERATIONS,
            )
            warnings.extend(compiler_warnings)
            document["wotbot:generation"]["operationKeys"] = list(operation_keys)
            document["wotbot:generation"]["operationCount"] = operation_count
            if actions:
                document["actions"] = actions
            else:
                warnings.append(
                    "The EDC asset OpenAPI metadata has no supported operations; "
                    "using the download fallback."
                )
        if "actions" not in document:
            action = provider_download_action(
                thing_id,
                action_name="download_asset",
                title="Download asset",
                description=(
                    "Negotiate access through the dataspace and return the asset content."
                ),
            )
            action["wotbot:generatedBy"] = self.name
            document["actions"] = {"download_asset": action}
        if (
            len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())
            > _MAX_TD_BYTES
        ):
            raise SourceProtocolError("Generated Thing Description is too large")
        return OnboardingResult(
            document=document,
            warnings=tuple(dict.fromkeys(warnings))[:20],
        )

    def _public_client(self, source: SourceDefinition) -> BoundedHttpClient | None:
        return (
            BoundedHttpClient(
                max_requests=self.public_max_requests,
                max_bytes=self.public_max_bytes,
            )
            if source.network_access == "public"
            else None
        )

    def _contract_request(
        self,
        source: SourceDefinition,
        dataset: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a negotiation request for this connector distribution."""

        del dataset
        return {
            "@context": JSONLD_CONTEXT,
            "@type": "ContractRequest",
            **self._party(source),
            "policy": policy,
        }

    async def acquire(
        self,
        source: SourceDefinition,
        *,
        external_id: str,
        title: str,
        resource_id: str | None,
        public_http: BoundedHttpClient | None = None,
    ) -> tuple[DownloadRecord, int | None]:
        del resource_id
        dataset = await self._selected_dataset(
            source,
            external_id,
            public_http=public_http,
        )
        policy = self._policy(dataset)
        if not policy:
            raise SourceProtocolError("The selected EDC asset has no current contract offer")
        negotiation = await source_json(
            "POST",
            f"{source.get('management_url')}/v3/contractnegotiations",
            source=source,
            public_http=public_http,
            body=self._contract_request(source, dataset, policy),
        )
        negotiation_id = response_id(negotiation, "id")
        negotiated = await self._poll(
            source,
            f"{source.get('management_url')}/v3/contractnegotiations/{quote(negotiation_id, safe='')}",
            success={"FINALIZED", "VERIFIED"},
            failure={"TERMINATED", "ERROR"},
            public_http=public_http,
        )
        agreement_id = text(
            field(negotiated, "contractAgreementId", "agreementId")
            or (field(negotiated, "contractAgreement") or {}).get("@id")
        )
        if not agreement_id:
            raise SourceProtocolError("EDC negotiation finalized without a contract agreement id")
        transfer = await source_json(
            "POST",
            f"{source.get('management_url')}/v3/transferprocesses",
            source=source,
            public_http=public_http,
            body={
                "@context": JSONLD_CONTEXT,
                "@type": "TransferRequest",
                **self._party(source),
                "contractId": agreement_id,
                "transferType": "HttpData-PULL",
            },
        )
        transfer_id = response_id(transfer, "id")
        await self._poll(
            source,
            f"{source.get('management_url')}/v3/transferprocesses/{quote(transfer_id, safe='')}",
            success={"STARTED", "COMPLETED"},
            failure={"TERMINATED", "DEPROVISIONED", "ERROR"},
            public_http=public_http,
        )
        address = await source_json(
            "GET",
            f"{source.get('management_url')}/v3/edrs/{quote(transfer_id, safe='')}/dataaddress",
            source=source,
            public_http=public_http,
        )
        endpoint = text(field(address, "endpoint", "baseUrl"))
        headers = secret_headers(address)
        parsed_endpoint = urlparse(endpoint)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.hostname
            or parsed_endpoint.username
            or parsed_endpoint.password
        ):
            raise SourceProtocolError("EDC returned an invalid endpoint data reference URL")
        ttl = edr_ttl(address)
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-") or "download"
        return (
            DownloadRecord(
                endpoint=endpoint,
                headers=headers,
                public=public_http is not None,
                filename=filename,
            ),
            ttl,
        )

    async def invoke_api(
        self,
        source: SourceDefinition,
        *,
        external_id: str,
        method: str,
        path: str,
        path_variables: tuple[str, ...],
        query_variables: tuple[str, ...],
        uri_variables: dict[str, Any],
        input_data: Any,
        public_http: BoundedHttpClient | None = None,
    ) -> ProviderResponse:
        capability, _ttl = await self.acquire(
            source,
            external_id=external_id,
            title="API response",
            resource_id=None,
            public_http=public_http,
        )
        target = _api_operation_url(
            capability.endpoint,
            path=path,
            path_variables=path_variables,
            query_variables=query_variables,
            uri_variables=uri_variables,
        )
        client = source_client(
            source,
            max_requests=4,
            max_bytes=_MAX_API_RESPONSE_BYTES,
            timeout_seconds=30,
        )
        response = await client.request(
            method,
            target,
            headers={"Accept": "application/json", **capability.headers},
            json_body=None if method in {"GET", "HEAD"} else input_data,
            max_bytes=_MAX_API_RESPONSE_BYTES,
            credentialed=bool(capability.headers),
        )
        if response.status < 200 or response.status >= 300:
            raise SourceProtocolError(f"EDC API operation returned HTTP {response.status}")
        return ProviderResponse(
            body=response.body,
            content_type=response.content_type or "application/json",
        )

    @staticmethod
    def _policy(dataset: dict[str, Any]) -> dict[str, Any] | None:
        policies = field(dataset, "odrl:hasPolicy", "hasPolicy") or []
        if isinstance(policies, dict):
            policies = [policies]
        return next(
            (
                item
                for item in policies
                if isinstance(item, dict)
                and isinstance(item.get("@id") or item.get("id"), str)
                and str(item.get("@id") or item.get("id")).strip()
            ),
            None,
        )

    async def _poll(
        self,
        source: SourceDefinition,
        url: str,
        *,
        success: set[str],
        failure: set[str],
        public_http: BoundedHttpClient | None = None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + float(
            source.get("poll_timeout_seconds", 120)
        )
        while asyncio.get_running_loop().time() < deadline:
            payload = await source_json(
                "GET",
                url,
                source=source,
                public_http=public_http,
            )
            if not isinstance(payload, dict):
                raise SourceProtocolError("EDC returned an invalid process state")
            state = text(field(payload, "state", "stateName")).upper()
            if state in success:
                return payload
            if state in failure:
                raise SourceProtocolError(f"EDC process failed in state '{state}'")
            await asyncio.sleep(float(source.get("poll_interval_seconds", 2)))
        raise SourceUnavailableError("EDC process timed out")


def catalog_datasets(catalog: Any) -> list[dict[str, Any]]:
    """Read catalog wrappers and the direct DatasetRequest response shape."""

    # A connector may answer with a single catalog or a list of them, and a
    # DatasetRequest returns the selected dataset directly.
    catalogs = catalog if isinstance(catalog, list) else [catalog]
    datasets: list[Any] = []
    for entry in catalogs:
        if not isinstance(entry, dict):
            continue
        found = field(entry, "dcat:dataset", "dataset")
        if found is None:
            if str(entry.get("@id") or entry.get("id") or "").strip():
                datasets.append(entry)
            continue
        datasets.extend(found if isinstance(found, list) else [found])
    return [item for item in datasets if isinstance(item, dict)]


def edc_api_description(dataset: dict[str, Any]) -> EdcApiDescription:
    """Parse tx-bootstrap's endpoint-neutral OpenAPI catalog metadata."""

    raw = _api_description_value(dataset)
    if raw is _MISSING:
        return EdcApiDescription(fingerprint="absent")
    literal = _jsonld_literal(raw)
    invalid_fingerprint_value = raw if literal is _MISSING else literal
    try:
        if isinstance(literal, str):
            body = literal.encode("utf-8")
            value = json.loads(literal)
        else:
            body = json.dumps(
                literal,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
            value = literal
        if len(body) > _MAX_OPENAPI_BYTES:
            raise OpenApiError("OpenAPI specification is too large")
        if not isinstance(value, dict):
            raise OpenApiError("OpenAPI specification must be an object")
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        document = parse_openapi(canonical)
        _validate_endpoint_neutral_openapi(document)
        digest = hashlib.sha256(canonical).hexdigest()
        return EdcApiDescription(
            fingerprint=f"openapi:{digest}",
            summary=_openapi_summary(document, digest=digest),
            document=document,
        )
    except (OpenApiError, TypeError, ValueError, json.JSONDecodeError, UnicodeEncodeError):
        invalid_bytes = _invalid_metadata_bytes(invalid_fingerprint_value)
        digest = hashlib.sha256(invalid_bytes).hexdigest()
        return EdcApiDescription(
            fingerprint=f"invalid:{digest}",
            warning=(
                "The EDC asset contains invalid OpenAPI metadata; using the download fallback."
            ),
        )


def _api_description_value(dataset: dict[str, Any]) -> Any:
    for container in (
        dataset,
        field(dataset, "properties"),
        dataset.get(EDC_PROPERTIES_URI),
    ):
        if not isinstance(container, dict):
            continue
        for name in (TXB_API_DESCRIPTION, TXB_API_DESCRIPTION_URI):
            if name in container:
                return container[name]
    return _MISSING


def _dataset_metadata(dataset: dict[str, Any], *, fallback: str) -> tuple[str, str]:
    containers = (
        dataset,
        field(dataset, "properties"),
        dataset.get(EDC_PROPERTIES_URI),
    )
    title = ""
    description = ""
    abstract = ""
    for container in containers:
        if not isinstance(container, dict):
            continue
        title = title or text(
            field(
                container,
                "dct:title",
                "http://purl.org/dc/terms/title",
                "title",
                "name",
            )
        )
        description = description or text(
            field(
                container,
                "dct:description",
                "http://purl.org/dc/terms/description",
                "description",
            )
        )
        abstract = abstract or text(
            field(
                container,
                "dct:abstract",
                "http://purl.org/dc/terms/abstract",
                "abstract",
            )
        )
    # Some tx-bootstrap assets put an entire YAML OpenAPI document in
    # dct:description as well as the canonical txb:apiDescription property.
    # Preserve a human-authored description, but never copy a raw spec into a
    # candidate or Thing merely because it occupies that metadata field.
    if _looks_like_openapi_document(description):
        description = abstract
    return _bounded_text(title or fallback, 500), _bounded_text(description or abstract, 2_000)


def _looks_like_openapi_document(value: str) -> bool:
    if not value:
        return False
    try:
        parse_openapi(value.encode("utf-8"))
    except (OpenApiError, UnicodeEncodeError):
        return False
    return True


def _jsonld_literal(value: Any) -> Any:
    if isinstance(value, list):
        for item in value:
            literal = _jsonld_literal(item)
            if literal is not _MISSING:
                return literal
        return _MISSING
    if isinstance(value, dict) and "@value" in value:
        return _jsonld_literal(value["@value"])
    return value


def _invalid_metadata_bytes(value: Any) -> bytes:
    try:
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace")
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    except (TypeError, ValueError, UnicodeEncodeError):
        return type(value).__name__.encode()


def _validate_endpoint_neutral_openapi(document: dict[str, Any]) -> None:
    version = str(document.get("openapi") or "")
    if not re.fullmatch(r"3\.(?:0|1)\.\d+(?:[-+].*)?", version):
        raise OpenApiError("EDC API description must declare OpenAPI 3.0 or 3.1")
    info = document.get("info")
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("title"), str)
        or not isinstance(info.get("version"), str)
    ):
        raise OpenApiError("OpenAPI info must contain title and version strings")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise OpenApiError("OpenAPI specification has no paths object")
    for path, path_item in paths.items():
        if not isinstance(path, str) or not _safe_api_path(path):
            raise OpenApiError("OpenAPI operation path is unsafe")
        _validate_path_item_reference(path_item)
    components = document.get("components")
    path_items = components.get("pathItems") if isinstance(components, dict) else None
    if isinstance(path_items, dict):
        for path_item in path_items.values():
            _validate_path_item_reference(path_item)
    _validate_openapi_nodes(document)


def _validate_path_item_reference(value: Any) -> None:
    if not isinstance(value, dict) or "$ref" not in value:
        return
    reference = value.get("$ref")
    if not isinstance(reference, str) or not re.fullmatch(
        r"#/(?:paths|components/pathItems)/[^/]+", reference
    ):
        raise OpenApiError("OpenAPI path item has an unsupported reference")


def _validate_openapi_nodes(value: Any) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, child in current.items():
            if key == "__proto__" or key in {"$id", "$schema", "externalValue"}:
                raise OpenApiError("OpenAPI metadata contains an unsafe field")
            if key in {"$ref", "$dynamicRef", "$recursiveRef", "operationRef"} and (
                not isinstance(child, str) or not child.startswith("#/")
            ):
                raise OpenApiError("External OpenAPI references are unsupported")
            stack.append(child)
        discriminator = current.get("discriminator")
        mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
        if isinstance(mapping, dict):
            for target in mapping.values():
                if not isinstance(target, str) or not (
                    target.startswith("#/") or re.fullmatch(r"[A-Za-z0-9._-]+", target)
                ):
                    raise OpenApiError("External discriminator mappings are unsupported")


def _safe_api_path(path: str) -> bool:
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        return False
    decoded = path
    for _ in range(10):
        if re.search(r"%(?![0-9A-Fa-f]{2})", decoded):
            return False
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        return False
    return (
        decoded.startswith("/")
        and not decoded.startswith("//")
        and not any(character in decoded for character in ("\\", "?", "#"))
        and not any(segment in {".", ".."} for segment in decoded.split("/"))
    )


def _api_operation_url(
    endpoint: str,
    *,
    path: str,
    path_variables: tuple[str, ...],
    query_variables: tuple[str, ...],
    uri_variables: dict[str, Any],
) -> str:
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.query or parsed_endpoint.fragment or not _safe_api_path(path):
        raise SourceProtocolError("EDC returned an invalid API endpoint")

    resolved_path = path
    for name in path_variables:
        if name not in uri_variables:
            raise ValueError(f"API operation requires URI variable '{name}'")
        resolved_path = resolved_path.replace(
            "{" + name + "}",
            quote(_uri_variable_text(uri_variables[name]), safe=""),
        )
    if "{" in resolved_path or "}" in resolved_path:
        raise ValueError("API operation has unresolved path variables")

    query = [
        (name, _uri_variable_text(uri_variables[name]))
        for name in query_variables
        if name in uri_variables
    ]
    target = endpoint.rstrip("/") + resolved_path
    if query:
        target += "?" + urlencode(query)
    if len(target) > 8_192:
        raise ValueError("API operation URL is too long")
    return target


def _uri_variable_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _openapi_summary(document: dict[str, Any], *, digest: str) -> dict[str, Any]:
    operations: list[dict[str, str]] = []
    raw_paths = document.get("paths")
    paths = raw_paths if isinstance(raw_paths, dict) else {}
    for path in sorted(paths, key=str):
        path_item = _resolve_path_item(document, paths[path])
        for method in _HTTP_METHODS:
            operation = path_item.get(method) if isinstance(path_item, dict) else None
            if not isinstance(operation, dict):
                continue
            operation_id = _bounded_text(operation.get("operationId"), 200)
            title = _bounded_text(operation.get("summary"), 500) or operation_id
            entry = {
                "method": method.upper(),
                "path": str(path)[:2_000],
                "operationId": operation_id,
                "title": title,
                "description": _bounded_text(operation.get("description"), 1_000),
            }
            operations.append(entry)
            if len(operations) > _MAX_OPENAPI_OPERATIONS:
                raise OpenApiError("OpenAPI specification contains too many operations")
    raw_info = document.get("info")
    info = raw_info if isinstance(raw_info, dict) else {}
    return {
        "@type": "wotbot:OpenApiDescription",
        "wotbot:generatedBy": "edc-v3",
        "format": "openapi",
        "specificationVersion": str(document.get("openapi"))[:100],
        "version": _bounded_text(info.get("version"), 200),
        "digest": f"sha256:{digest}",
        "title": _bounded_text(info.get("title"), 500),
        "description": _bounded_text(info.get("description"), 2_000),
        "operationCount": len(operations),
        "operations": operations[:_MAX_SUMMARY_OPERATIONS],
        "truncated": len(operations) > _MAX_SUMMARY_OPERATIONS,
    }


def _resolve_path_item(document: dict[str, Any], value: Any) -> dict[str, Any]:
    current = value
    seen: set[str] = set()
    while isinstance(current, dict) and "$ref" in current:
        reference = str(current.get("$ref") or "")
        if reference in seen or len(seen) >= 20 or not reference.startswith("#/"):
            raise OpenApiError("OpenAPI path item reference is cyclic or invalid")
        seen.add(reference)
        resolved: Any = document
        for part in reference[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or key not in resolved:
                raise OpenApiError("OpenAPI path item reference is unresolved")
            resolved = resolved[key]
        if not isinstance(resolved, dict):
            raise OpenApiError("OpenAPI path item reference is invalid")
        current = {**resolved, **{key: item for key, item in current.items() if key != "$ref"}}
    if not isinstance(current, dict):
        raise OpenApiError("OpenAPI path item is invalid")
    return current


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _is_legacy_download_action(value: Any, *, thing_id: str) -> bool:
    if not isinstance(value, dict) or value.get("wotbot:generatedBy") is not None:
        return False
    forms = value.get("forms")
    return bool(
        thing_id
        and isinstance(forms, list)
        and any(
            isinstance(form, dict)
            and form.get("href") == provider_action_href(thing_id, "download_asset")
            and form.get("wotbot:providerOperation") == "download"
            for form in forms
        )
    )


def _with_legacy_edc_markers(document: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(document)
    thing_id = str(current.get("id") or "")
    actions = current.get("actions")
    legacy = actions.get("download_asset") if isinstance(actions, dict) else None
    if (
        not isinstance(actions, dict)
        or not isinstance(legacy, dict)
        or not _is_legacy_download_action(legacy, thing_id=thing_id)
    ):
        return current
    current_actions = dict(actions)
    current_actions["download_asset"] = {
        **legacy,
        "wotbot:generatedBy": "edc-v3",
    }
    current["actions"] = current_actions
    definitions = current.get("securityDefinitions")
    nosec = definitions.get("nosec_sc") if isinstance(definitions, dict) else None
    if isinstance(definitions, dict) and nosec == {"scheme": "nosec"}:
        current_definitions = dict(definitions)
        current_definitions["nosec_sc"] = {
            "scheme": "nosec",
            "wotbot:generatedBy": "edc-v3",
        }
        current["securityDefinitions"] = current_definitions
    return current


def field(payload: Any, *names: str) -> Any:
    """Read the first present field, accepting the ``edc:`` JSON-LD prefix.

    Whether a management API expands or compacts its response depends on the
    distribution and its version, so the same value arrives as ``state`` or
    ``edc:state`` from connectors that are otherwise identical.
    """

    if not isinstance(payload, dict):
        return None
    for name in names:
        for key in (name, f"{EDC_PREFIX}{name}"):
            if payload.get(key) is not None:
                return payload[key]
    return None


def response_id(payload: Any, fallback: str) -> str:
    if not isinstance(payload, dict):
        raise SourceProtocolError("EDC returned an invalid process response")
    value = text(payload.get("@id") or payload.get("id"))
    if not value:
        raise SourceProtocolError(f"EDC response is missing {fallback}")
    return value


def secret_headers(address: Any) -> dict[str, str]:
    """Build the single header that authorizes the acquired endpoint.

    ``authKey`` is ambiguous across distributions: a plain EDC uses it for the
    header *name* alongside ``authCode``, while Tractus-X uses it as a fallback
    for the token itself. The presence of a separate value field decides which,
    so a token is never mistaken for a header name.
    """

    if not isinstance(address, dict):
        raise SourceProtocolError("EDC returned an incomplete endpoint data reference")
    separate_value = text(field(address, "authCode", "authorization"))
    if separate_value:
        header_name = text(field(address, "authKey")) or "Authorization"
        header_value = separate_value
    else:
        # No separate value: Tractus-X puts the token in authKey. A bare header
        # name there means the reference simply arrived without its secret.
        header_name = "Authorization"
        candidate = text(field(address, "authKey"))
        header_value = "" if candidate.casefold() in AUTH_HEADER_NAMES else candidate
    if not header_value:
        raise SourceProtocolError("EDC returned an incomplete endpoint data reference")
    if (
        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header_name)
        or header_name.casefold() in FORBIDDEN_SECRET_HEADERS
    ):
        raise SourceProtocolError("EDC returned an unsafe authorization header name")
    return {header_name: header_value}


def edr_ttl(address: dict[str, Any], *, now: float | None = None) -> int | None:
    if "expiresIn" in address:
        try:
            ttl = int(float(address["expiresIn"]))
        except (TypeError, ValueError) as exc:
            raise SourceProtocolError(
                "EDC returned an invalid endpoint data reference expiration"
            ) from exc
        if ttl <= 0:
            raise SourceProtocolError("EDC endpoint data reference has expired")
        return ttl
    raw_expiry = next(
        (
            address[key]
            for key in ("expiresAt", "expiration", "expirationDate")
            if address.get(key) is not None
        ),
        None,
    )
    if raw_expiry is None:
        return None
    try:
        text_expiry = str(raw_expiry).strip()
        if isinstance(raw_expiry, str) and not text_expiry.replace(".", "", 1).isdigit():
            expires_at = datetime.fromisoformat(text_expiry).timestamp()
        else:
            expires_at = float(raw_expiry)
            if expires_at > 10_000_000_000:
                expires_at /= 1000
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceProtocolError(
            "EDC returned an invalid endpoint data reference expiration"
        ) from exc
    ttl = int(expires_at - (time.time() if now is None else now))
    if ttl <= 0:
        raise SourceProtocolError("EDC endpoint data reference has expired")
    return ttl
