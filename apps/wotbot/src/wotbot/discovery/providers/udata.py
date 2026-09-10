"""uData: datasets published by a portal running the uData software.

Expects a uData v1 API rooted at the source URL's origin, so that
``GET {origin}/api/1/datasets/?q=&page_size=`` answers with an object carrying
a ``data`` array. Each dataset needs an ``id`` (or ``slug``) and a ``title``;
``resources`` supplies the downloadable distributions.

Configuration
    ``url``
        Any URL on the portal. Only its origin is used. Required.
    ``portal_url``
        The human-facing page to link back to. Optional.

The query is pushed to the backend rather than filtered here: intent is
compiled into at most three short full-text probes (see ``udata_queries``),
their results are merged by dataset id, and the local ranking only reorders
what the portal already selected. That keeps result quality tied to the
portal's own index instead of to whatever happened to be on the first page.

Each dataset becomes one Thing whose distributions become download actions.
Resources typed ``api`` or ``documentation`` are treated as links rather than
downloads: they appear in the TD's ``links`` and are refused by ``acquire``,
because following them would fetch a landing page instead of data. Onboarding
also reports them as suggested sources, so a dataset that only points at a
service is a signpost to source detection rather than an affordance-less Thing.

Acquisition re-reads ``GET /api/1/datasets/{id}/`` and matches the stored
resource id, so a distribution that was withdrawn upstream fails loudly rather
than resolving to a stale URL. Detection needs the portal homepage and is
tried after the more specific probes; there is no refresh.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from wotbot.discovery.detection import DetectionContext, origin
from wotbot.discovery.errors import (
    ProviderError,
    SourceProtocolError,
    SourceResponseTooLargeError,
)
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
    OnboardingRuntime,
    dataset_document,
    items,
    source_json,
    stable_resource_id,
    text,
)
from wotbot.discovery.search import prepare_search_intent, rank_candidates

_FORMAT_MEDIA_TYPES = {
    "html": "text/html",
    "json": "application/json",
}
_LINK_RESOURCE_TYPES = {"api", "documentation"}
_SEARCH_REQUEST_LIMIT = 10


def resources(value: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("latest") or "").strip()
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            continue
        path_name = PurePosixPath(parsed.path).name
        format_name = str(item.get("format") or "")[:100]
        media_type = str(item.get("mime") or "")[:200]
        if not media_type and "/" in format_name:
            media_type = format_name
        if not media_type:
            media_type = _FORMAT_MEDIA_TYPES.get(format_name.casefold(), "")
        descriptor: dict[str, Any] = {
            "id": stable_resource_id(url, str(item.get("id") or "")),
            "title": str(item.get("title") or item.get("description") or path_name or "Download")[
                :200
            ],
            "description": str(item.get("description") or "")[:1000],
            "url": url,
            "media_type": media_type,
            "format": format_name,
            "filename": str(item.get("filename") or path_name or "download")[:200],
            "modified": str(item.get("last_modified") or item.get("modified") or "")[:100],
            "checksum": text(item.get("checksum"))[:200],
            "resource_type": str(item.get("type") or "").casefold()[:50],
            "file_type": str(item.get("filetype") or "").casefold()[:50],
        }
        raw_size = item.get("filesize")
        if raw_size is None:
            raw_size = item.get("size")
        if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0:
            descriptor["size_bytes"] = raw_size
        results.append(descriptor)
        if len(results) >= limit:
            break
    return results


def public_links(
    dataset: dict[str, Any], descriptors: list[dict[str, Any]]
) -> tuple[dict[str, str], ...]:
    links: list[dict[str, str]] = []
    page = str(dataset.get("page") or dataset.get("uri") or "").strip()
    if page:
        links.append({"title": "Dataset page", "url": page, "media_type": "text/html"})
    links.extend(
        {
            "title": item["title"],
            "url": item["url"],
            "media_type": item["media_type"],
        }
        for item in descriptors
    )
    return tuple(links[:6])


def service_suggestions(descriptors: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """Service endpoints from this dataset worth registering as their own source.

    uData types a resource ``api`` for any service endpoint, so the type says
    where a service is, not what it speaks. It may refer to WMS capabilities,
    pygeoapi collections, or an OpenAPI document. Source detection determines
    support by probing the URL. A dataset just reports
    that a service exists, and documentation pages are included because a docs
    page is often what names the specification behind the endpoint.
    """

    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in descriptors:
        resource_type = str(item.get("resource_type") or "").casefold()
        url = str(item.get("url") or "")
        if resource_type not in _LINK_RESOURCE_TYPES or not url or url in seen:
            continue
        seen.add(url)
        suggestion = {
            "url": url,
            "title": str(item.get("title") or "Service endpoint"),
            "kind": resource_type,
        }
        if format_name := str(item.get("format") or ""):
            suggestion["format"] = format_name
        suggestions.append(suggestion)
    return tuple(suggestions[:5])


class UdataProvider(DiscoveryProvider):
    name = "udata"
    capabilities = ("detect", "search", "onboard")
    detect_priority = 50
    # Resource histories can make even a small dataset page several MiB.
    # Shrink oversized pages while retaining the per-response byte cap.
    public_max_bytes = 4 * 1024 * 1024
    public_max_requests = _SEARCH_REQUEST_LIMIT + 2  # Allow a couple of redirects.
    config = ProviderConfigSpec(
        fields=frozenset({"url", "portal_url"}),
        url_fields=("url",),
        optional_url_fields=("portal_url",),
        title="uData catalog",
    )

    async def inspect_public(self, context: DetectionContext) -> SourceDefinition | None:
        homepage = await context.homepage()
        if not homepage.ok:
            return None
        hinted = "udata" in homepage.payload.text().casefold() or any(
            "/api/1" in link for link in homepage.links
        )
        source = SourceDefinition(
            id=homepage.root,
            external_id=homepage.root,
            provider=self.name,
            config={"url": homepage.root, "portal_url": homepage.payload.url},
            **homepage.metadata(),  # type: ignore[arg-type]
        )
        try:
            await self.search(
                source, prepare_search_intent("", source), 1, public_http=context.http
            )
        except ProviderError as exc:
            context.note(f"uData probe failed: {exc}")
            return None
        context.note(
            "Detected a working uData API at /api/1"
            + (" from page metadata" if hinted else " by bounded endpoint probe")
        )
        return source

    async def _json(
        self,
        source: SourceDefinition,
        url: str,
        *,
        public_http: BoundedHttpClient | None,
    ) -> Any:
        return await source_json("GET", url, source=source, public_http=public_http)

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        root = origin(str(source.get("url")))
        page_size = 10
        remaining_requests = _SEARCH_REQUEST_LIMIT
        datasets: dict[str, dict[str, Any]] = {}
        for query in udata_queries(intent):
            offset = 0
            while remaining_requests:
                # Rounding down can repeat a dataset after a page-size change;
                # deduplication below preserves it without skipping any records.
                page = offset // page_size + 1
                params = urlencode({"q": query, "page_size": page_size, "page": page})
                url = f"{root}/api/1/datasets/?{params}"
                remaining_requests -= 1
                try:
                    payload = await self._json(source, url, public_http=public_http)
                except SourceResponseTooLargeError:
                    if page_size == 1 or not remaining_requests:
                        raise
                    page_size = max(1, page_size // 2)
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise SourceProtocolError("uData API returned an invalid dataset listing")
                for dataset in items(payload):
                    external_id = str(dataset.get("id") or dataset.get("slug") or "").strip()
                    if external_id:
                        datasets.setdefault(external_id, dataset)
                if len(datasets) >= limit or not payload["data"] or not payload.get("next_page"):
                    break
                offset = page * page_size
            if len(datasets) >= limit or not remaining_requests:
                break

        candidates: list[tuple[CandidateDraft, str]] = []
        for external_id, dataset in datasets.items():
            title = text(dataset.get("title") or dataset.get("name"))
            if not title:
                continue
            summary = text(dataset.get("description"))
            descriptors = resources(dataset.get("resources"))
            candidate = CandidateDraft(
                provider=self.name,
                source_id=source.id,
                external_id=external_id,
                kind="dataset",
                title=title,
                summary=summary,
                links=public_links(dataset, descriptors),
                payload={"resources": descriptors},
            )
            candidates.append((candidate, _dataset_search_text(dataset, descriptors)))
        return rank_candidates(intent, candidates, limit=limit, require_match=False)

    def suggest_sources(self, candidate: CandidateDraft) -> tuple[dict[str, str], ...]:
        raw = candidate.payload.get("resources")
        descriptors = (
            [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        )
        return service_suggestions(descriptors)

    async def onboarding_document(
        self,
        source: SourceDefinition,
        candidate: CandidateDraft,
        *,
        runtime: OnboardingRuntime,
    ) -> OnboardingResult:
        del source, runtime
        raw = candidate.payload.get("resources")
        descriptors = (
            [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        )
        return OnboardingResult(
            document=dataset_document(candidate, descriptors),
            suggested_sources=self.suggest_sources(candidate),
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
        if not resource_id:
            raise ValueError("resource_id is required")
        url = f"{origin(str(source.get('url')))}/api/1/datasets/{quote(external_id, safe='')}/"
        dataset = await self._json(source, url, public_http=public_http)
        if not isinstance(dataset, dict):
            raise SourceProtocolError("uData API returned an invalid dataset")
        descriptor = next(
            (item for item in resources(dataset.get("resources")) if item["id"] == resource_id),
            None,
        )
        if descriptor is None:
            raise SourceProtocolError("The selected dataset resource is no longer available")
        if descriptor.get("resource_type") in _LINK_RESOURCE_TYPES:
            raise SourceProtocolError(
                "The selected dataset resource is a link and cannot be downloaded"
            )
        path_name = PurePosixPath(urlparse(descriptor["url"]).path).name
        fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-") or "download"
        return (
            DownloadRecord(
                endpoint=descriptor["url"],
                public=public_http is not None,
                content_type=descriptor["media_type"] or "application/octet-stream",
                filename=path_name or fallback,
                size_bytes=descriptor.get("size_bytes"),
            ),
            None,
        )


def udata_queries(intent: SearchIntent) -> tuple[str, ...]:
    """Compile natural intent into at most three short uData full-text probes."""

    if not intent.original:
        return ("",)

    entity = intent.entities[0] if intent.entities else ""
    entity_words = {word.casefold() for word in re.findall(r"[^\W_]+", entity)}
    topics = [keyword for keyword in intent.keywords if keyword.casefold() not in entity_words]
    queries: list[str] = []
    if entity:
        queries.append(entity)
        if topics:
            queries.append(f"{entity} {topics[0]}")
        if len(topics) >= 2:
            queries.append(f"{topics[0]} {topics[1]}")
    elif topics:
        queries.append(" ".join(topics[:2]))
        queries.append(topics[0])
        if len(topics) >= 3:
            queries.append(" ".join(topics[1:3]))

    unique: list[str] = []
    seen: set[str] = set()
    for query in queries or [intent.original]:
        normalized = query.casefold().strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(query[:100])
    return tuple(unique[:3])


def _dataset_search_text(dataset: dict[str, Any], descriptors: list[dict[str, Any]]) -> str:
    organization = dataset.get("organization")
    tags = dataset.get("tags")
    values = [
        text(dataset.get("title") or dataset.get("name")),
        text(dataset.get("description")),
        text(organization),
        *(text(tag) for tag in (tags if isinstance(tags, list) else [])),
        *(str(item.get("title") or "") for item in descriptors),
        *(str(item.get("format") or "") for item in descriptors),
    ]
    return " ".join(value for value in values if value)
