from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from abc import ABC, abstractmethod
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from wotbot.discovery.detection import DetectionContext
from wotbot.discovery.errors import (
    SourceAuthenticationError,
    SourceProtocolError,
    SourceUnavailableError,
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
from wotbot.discovery.refresh import generated_diff, merge_generated_document

TD_CONTEXT = "https://www.w3.org/2022/wot/td/v1.1"
WOTBOT_CONTEXT = {"wotbot": "https://wotbot.dev/ontology#"}
PROVIDER_SCHEME = "wotbot+provider"


class OnboardingRuntime(Protocol):
    async def describe_endpoint(self, *, url: str) -> dict[str, Any]: ...


class DiscoveryProvider(ABC):
    name: str
    config: ProviderConfigSpec
    capabilities: tuple[str, ...] = ("search", "onboard")
    # Lower runs first. A probe that recognizes an exact document type must be
    # tried before one that guesses from a portal homepage.
    detect_priority: int = 100
    public_max_requests: int = 5
    public_max_bytes: int = 1_048_576

    @abstractmethod
    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]: ...

    @abstractmethod
    async def onboarding_document(
        self,
        source: SourceDefinition,
        candidate: CandidateDraft,
        *,
        runtime: OnboardingRuntime,
    ) -> OnboardingResult: ...

    def suggest_sources(self, candidate: CandidateDraft) -> tuple[dict[str, str], ...]:
        """Return related source URLs from staged metadata without probing or onboarding."""

        del candidate
        return ()

    @property
    def generation_marker(self) -> str:
        """The ``wotbot:generatedBy`` stamp this provider puts on what it owns."""

        return self.name

    @property
    def default_security_scheme(self) -> str:
        configured = self.config.default_security_scheme.strip().lower()
        if configured:
            return configured
        return "apikey" if self.config.requires_secret else "nosec"

    def merge_refresh(
        self,
        current_document: dict[str, Any],
        generated_document: dict[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Overlay a regenerated document, preserving hand-authored additions."""

        return merge_generated_document(
            current_document,
            generated_document,
            marker=self.generation_marker,
        )

    def refresh_diff(
        self,
        current_document: dict[str, Any],
        replacement: dict[str, Any],
    ) -> dict[str, Any]:
        """Summarize what applying a refresh would change."""

        return generated_diff(current_document, replacement, marker=self.generation_marker)

    async def refresh_document(
        self,
        source: SourceDefinition,
        current_document: dict[str, Any],
        *,
        runtime: OnboardingRuntime,
        external_id: str = "",
    ) -> OnboardingResult:
        del source, current_document, external_id, runtime
        raise ValueError(f"Provider '{self.name}' does not support refresh")

    async def inspect_public(self, context: DetectionContext) -> SourceDefinition | None:
        """Return a source if this provider recognizes the probed URL.

        Raising a ProviderError means "not mine"; the registry records it as
        evidence and moves on to the next provider.
        """

        del context
        return None

    def normalize_config(self, config: dict[str, Any]) -> dict[str, Any]:
        unknown = set(config) - self.config.fields
        if unknown:
            raise ValueError("Unknown provider configuration fields: " + ", ".join(sorted(unknown)))
        normalized = dict(config)
        for field in self.config.url_fields:
            value = str(normalized.get(field) or "").strip().rstrip("/")
            if not is_http_endpoint(value):
                raise ValueError(f"Provider requires a valid '{field}' URL")
            normalized[field] = value
        for field in self.config.optional_url_fields:
            value = str(normalized.get(field) or "").strip().rstrip("/")
            if value and not is_http_endpoint(value):
                raise ValueError(f"Provider requires a valid '{field}' URL")
            normalized[field] = value
        for field, default in self.config.text_defaults:
            normalized[field] = str(normalized.get(field) or default).strip()
        for field, float_default in self.config.float_defaults:
            float_value = float(normalized.get(field) or float_default)
            if float_value <= 0:
                raise ValueError(f"Provider requires a positive '{field}'")
            normalized[field] = float_value
        return normalized

    def external_identity(self, config: dict[str, Any]) -> str:
        if not self.config.url_fields:
            raise ValueError(f"Provider '{self.name}' has no canonical URL field")
        return str(config.get(self.config.url_fields[0]) or "")

    async def acquire(
        self,
        source: SourceDefinition,
        *,
        external_id: str,
        title: str,
        resource_id: str | None,
        public_http: BoundedHttpClient | None = None,
    ) -> tuple[DownloadRecord, int | None]:
        raise ValueError(f"Provider '{self.name}' does not expose an acquire action")

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
        del (
            source,
            external_id,
            method,
            path,
            path_variables,
            query_variables,
            uri_variables,
            input_data,
            public_http,
        )
        raise ValueError(f"Provider '{self.name}' does not expose API operations")

    def registration_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            field: {"type": "string"} for field in sorted(self.config.fields)
        }
        for field in self.config.url_fields:
            properties[field] = {"type": "string", "format": "uri"}
        for field in self.config.optional_url_fields:
            properties[field] = {"type": "string", "format": "uri"}
        for field, default in self.config.text_defaults:
            properties[field] = {"type": "string", "default": default}
        for field, float_default in self.config.float_defaults:
            properties[field] = {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": float_default,
            }
        return {
            "provider": self.name,
            "title": self.config.title or self.name,
            "capabilities": list(self.capabilities),
            "config_schema": {
                "type": "object",
                "properties": properties,
                "required": sorted(self.config.url_fields),
                "additionalProperties": False,
            },
            "default_security_scheme": self.default_security_scheme,
            "security_schemes": ["nosec", "apikey", "bearer", "basic", "oauth2"],
        }


def source_client(source: SourceDefinition, **overrides: Any) -> BoundedHttpClient:
    """Build the client one source is allowed to fetch through."""

    settings: dict[str, Any] = {
        "mode": "public" if source.network_access == "public" else "trusted",
        "max_bytes": 4 * 1024 * 1024,
    }
    if source.network_access != "public":
        # A deliberately registered private source has no probe budget; EDC
        # acquisition alone polls far past any small fixed limit.
        settings["max_requests"] = None
    settings.update(overrides)
    return BoundedHttpClient(**settings)


async def source_json(
    method: str,
    url: str,
    *,
    source: SourceDefinition,
    public_http: BoundedHttpClient | None,
    body: dict[str, Any] | None = None,
    timeout: float = 10,
) -> Any:
    """Fetch and decode one JSON document under the source's network policy."""

    client = (
        public_http if public_http is not None else source_client(source, timeout_seconds=timeout)
    )
    secrets = credential_headers(source)
    response = await client.request(
        method,
        url,
        headers={"Accept": "application/json", **secrets},
        json_body=body,
        credentialed=bool(secrets),
    )
    if response.status in {401, 403}:
        raise SourceAuthenticationError(f"Discovery source '{source.id}' rejected its credential")
    if response.status < 200 or response.status >= 300:
        raise SourceUnavailableError(
            f"Discovery source '{source.id}' returned HTTP {response.status}"
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise SourceProtocolError(f"Discovery source '{source.id}' returned invalid JSON") from exc


async def trusted_json(
    method: str,
    url: str,
    *,
    source: SourceDefinition,
    body: dict[str, Any] | None = None,
    timeout: float = 10,
) -> Any:
    """Fetch JSON from a source that was registered as private."""

    return await source_json(
        method, url, source=source, public_http=None, body=body, timeout=timeout
    )


def items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    value = payload
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
    if isinstance(value, dict):
        for key in ("data", "items", "results", "workloads", "servers"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def text(value: Any) -> str:
    if isinstance(value, str):
        return str(value)
    if isinstance(value, list):
        return next((result for item in value if (result := text(item))), "")
    if isinstance(value, dict):
        for key in ("@value", "value", "name"):
            if key in value:
                return text(value[key])
    return "" if value is None else str(value)


def provider_thing_id(provider: str, source_id: str, external_id: str) -> str:
    identity = f"{provider}\0{source_id}\0{external_id}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return f"urn:wotbot:external:{provider}:{digest}"


def provider_action_href(thing_id: str, action: str) -> str:
    encoded = base64.urlsafe_b64encode(thing_id.encode()).decode().rstrip("=")
    return f"{PROVIDER_SCHEME}://runtime/things/{encoded}/actions/{quote(action, safe='')}"


def credential_headers(source: SourceDefinition) -> dict[str, str]:
    if source.security_scheme == "nosec":
        return {}
    credential = source.credential
    if source.security_scheme == "apikey":
        value = credential.get("apiKey") or credential.get("apikey")
        header = str(source.get("api_key_header", "X-Api-Key"))
        return {header: str(value)} if value else {}
    if source.security_scheme in {"bearer", "oauth2"}:
        value = credential.get("token") or credential.get("access_token")
        return {"Authorization": f"Bearer {value}"} if value else {}
    if source.security_scheme == "basic":
        username = credential.get("username")
        password = credential.get("password")
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
    return {}


def is_http_endpoint(value: str) -> bool:
    if not value or len(value) > 4096:
        return False
    parsed = urlparse(value)
    try:
        _port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def download_action_name(resource: dict[str, Any]) -> str:
    resource_id = str(resource.get("id") or "resource")
    label = str(
        resource.get("title") or resource.get("filename") or resource.get("format") or "resource"
    )
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_label).strip("_")[:40] or "resource"
    suffix = hashlib.sha256(resource_id.encode()).hexdigest()[:8]
    return f"download_{slug}_{suffix}"


def provider_download_action(
    thing_id: str,
    *,
    action_name: str,
    title: str,
    description: str,
    resource: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "@type": "wotbot:DownloadAction",
        "title": title,
        "description": description,
        "safe": False,
        "idempotent": False,
        "forms": [
            {
                "href": provider_action_href(thing_id, action_name),
                "op": ["invokeaction"],
                "contentType": "application/json",
                "response": {
                    "contentType": str(
                        (resource or {}).get("media_type")
                        or (
                            (resource or {}).get("format")
                            if "/" in str((resource or {}).get("format") or "")
                            else ""
                        )
                        or "application/octet-stream"
                    )
                },
                "wotbot:providerOperation": "download",
            }
        ],
    }
    if resource:
        action["forms"][0]["wotbot:resourceId"] = str(resource["id"])
        for source_key, td_key in (
            ("media_type", "wotbot:mediaType"),
            ("format", "wotbot:format"),
            ("filename", "wotbot:filename"),
            ("size_bytes", "wotbot:byteSize"),
            ("modified", "wotbot:modified"),
            ("checksum", "wotbot:checksum"),
        ):
            value = resource.get(source_key)
            if value is not None and value != "":
                action[td_key] = value
    return action


def dataset_document(candidate: CandidateDraft, resources: list[dict[str, Any]]) -> dict[str, Any]:
    thing_id = provider_thing_id(candidate.provider, candidate.source_id, candidate.external_id)
    linked_resource_urls = {
        str(resource.get("url") or "")
        for resource in resources
        if str(resource.get("resource_type") or "").casefold() in {"api", "documentation"}
    }
    links = [
        {
            "href": link["url"],
            "type": "text/html",
            "rel": "alternate",
        }
        for link in candidate.links
        if link.get("media_type") == "text/html" and link.get("url") not in linked_resource_urls
    ]
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "").casefold()
        if resource_type not in {"api", "documentation"}:
            continue
        link: dict[str, str] = {
            "href": str(resource["url"]),
            "rel": "service" if resource_type == "api" else "service-doc",
        }
        if media_type := str(resource.get("media_type") or "").strip():
            link["type"] = media_type
        links.append(link)
    document: dict[str, Any] = {
        "@context": [TD_CONTEXT, WOTBOT_CONTEXT],
        "id": thing_id,
        "title": candidate.title,
        "description": candidate.summary,
        "security": ["nosec_sc"],
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "links": links,
    }
    if resources:
        actions: dict[str, Any] = {}
        for resource in resources:
            if str(resource.get("resource_type") or "").casefold() in {
                "api",
                "documentation",
            }:
                continue
            action_name = download_action_name(resource)
            format_label = str(resource.get("format") or resource.get("media_type") or "resource")
            resource_title = str(resource.get("title") or resource.get("filename") or "resource")
            title = (
                resource_title
                if resource_title.casefold().startswith("download")
                else f"Download {resource_title}"
            )
            description = str(resource.get("description") or "").strip()
            if not description:
                details = [f"format: {format_label}"]
                if filename := str(resource.get("filename") or "").strip():
                    details.append(f"file: {filename}")
                if size_bytes := resource.get("size_bytes"):
                    details.append(f"size: {size_bytes} bytes")
                if modified := str(resource.get("modified") or "").strip():
                    details.append(f"modified: {modified}")
                description = f"Download this dataset distribution ({', '.join(details)})."
            actions[action_name] = provider_download_action(
                thing_id,
                action_name=action_name,
                title=title,
                description=description,
                resource=resource,
            )
        if actions:
            document["actions"] = actions
    return document


def stable_resource_id(url: str, upstream_id: str = "") -> str:
    if upstream_id.strip():
        return upstream_id.strip()[:200]
    return hashlib.sha256(url.encode()).hexdigest()[:24]
