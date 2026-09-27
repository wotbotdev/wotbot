"""WoT TDD: Thing Descriptions listed by a W3C WoT Thing Description Directory.

Implements the consumer side of the Directory Service API from W3C WoT
Discovery (https://www.w3.org/TR/wot-discovery/#exploration-directory-api):
``GET {url}/things`` lists the registered TDs and ``GET {url}/things/{id}``
retrieves one. The optional JSONPath, XPath and SPARQL search APIs are not
used, because a directory need not implement any of them.

Configuration
    ``url``
        The directory base, under which ``/things`` is served. Required.

Search pages through the listing with ``offset``/``limit``, following the
``Link: rel="next"`` header the spec defines, and ranks the TDs in this
process. A directory with more than ``_MAX_THINGS`` entries is refused rather
than truncated, since a truncated listing would silently hide matches. Every
listed TD is returned, ranked, even without a keyword match: a Thing's
vocabulary rarely matches the words someone searches with.

Onboarding fetches the chosen TD again and requires the identity it was listed
under. The TD is kept as its owner published it, so its semantic annotations
and direct forms survive; only the directory's own ``registration`` metadata
is dropped. TDs without an ``id`` are skipped, as they cannot be retrieved
again. There is no refresh.

Detection reads the introduction at ``/.well-known/wot`` and accepts a TD typed
``ThingDirectory``, taking the base from its ``things`` property form. Other
introduction mechanisms (DNS-SD, CoRE Link Format, DID documents) are not
probed.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode, urljoin

from wotbot.discovery.detection import DetectionContext, origin
from wotbot.discovery.errors import (
    SourceAuthenticationError,
    SourceProtocolError,
    SourceUnavailableError,
    StaleCandidateError,
)
from wotbot.discovery.http import BoundedHttpClient, HttpPayload, origin_of
from wotbot.discovery.models import (
    CandidateDraft,
    OnboardingResult,
    ProviderConfigSpec,
    SearchIntent,
    SourceDefinition,
)
from wotbot.discovery.providers.base import (
    DiscoveryProvider,
    OnboardingRuntime,
    credential_headers,
    is_http_endpoint,
    source_client,
    text,
)
from wotbot.discovery.search import rank_candidates

_MAX_BYTES = 4 * 1024 * 1024
_PAGE_SIZE = 100
_MAX_THINGS = 500
_ACCEPT = "application/td+json, application/ld+json;q=0.9, application/json;q=0.8"
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;[^,]*?\brel="?next"?', re.IGNORECASE)
_AFFORDANCES = ("properties", "actions", "events")


def next_link(payload: HttpPayload) -> str | None:
    """Return the absolute ``rel="next"`` target of a listing page, if any."""

    match = _NEXT_LINK.search(payload.headers.get("link", ""))
    return urljoin(payload.url, match.group(1)) if match else None


def _types(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, str)]


def _labels(node: dict[str, Any]) -> list[str]:
    """Human-readable text of one TD or affordance, in every language given."""

    labels = [text(node.get("title")), text(node.get("description")), *_types(node.get("@type"))]
    for key in ("titles", "descriptions"):
        translations = node.get(key)
        if isinstance(translations, dict):
            labels.extend(text(value) for value in translations.values())
    return [label for label in labels if label]


def search_text(td: dict[str, Any]) -> str:
    """The text a TD is ranked on: its own labels and those of its affordances."""

    parts = [str(td["id"]), *_labels(td)]
    for kind in _AFFORDANCES:
        affordances = td.get(kind)
        if not isinstance(affordances, dict):
            continue
        for name, affordance in list(affordances.items())[:100]:
            parts.append(str(name))
            if isinstance(affordance, dict):
                parts.extend(_labels(affordance))
    return " ".join(parts)


def is_directory_td(document: Any) -> bool:
    return isinstance(document, dict) and any(
        item.rsplit(":", 1)[-1] == "ThingDirectory" for item in _types(document.get("@type"))
    )


def directory_base(document: dict[str, Any], document_url: str) -> str | None:
    """Resolve the directory base from its TD's ``things`` property form."""

    things = (document.get("properties") or {}).get("things")
    forms = things.get("forms") if isinstance(things, dict) else None
    if not isinstance(forms, list):
        return None
    base = (
        urljoin(document_url, document["base"])
        if isinstance(document.get("base"), str)
        else document_url
    )
    for form in forms:
        href = form.get("href") if isinstance(form, dict) else None
        if not isinstance(href, str):
            continue
        listing = urljoin(base, href).split("?", 1)[0].rstrip("/")
        if listing.endswith("/things"):
            return listing[: -len("/things")]
    return None


class WotTddProvider(DiscoveryProvider):
    name = "wot-tdd"
    capabilities = ("detect", "search", "onboard")
    # The introduction names its own document type, so it is recognized exactly
    # and costs one request; only ToolHive's probe is more specific.
    detect_priority = 30
    public_max_bytes = _MAX_BYTES
    public_max_requests = _MAX_THINGS // _PAGE_SIZE + 3  # Allow a couple of redirects.
    config = ProviderConfigSpec(
        fields=frozenset({"url"}),
        url_fields=("url",),
        title="WoT Thing Description Directory",
    )

    async def inspect_public(self, context: DetectionContext) -> SourceDefinition | None:
        introduction = f"{context.root}/.well-known/wot"
        payload = await context.http.get(introduction, headers={"Accept": _ACCEPT})
        if not 200 <= payload.status < 300:
            context.note(f"No WoT introduction at {introduction} (HTTP {payload.status})")
            return None
        try:
            document = payload.json()
        except ValueError:
            return None
        if not is_directory_td(document):
            context.note(f"{introduction} does not describe a Thing Description Directory")
            return None
        base = directory_base(document, payload.url)
        if not base or not is_http_endpoint(base) or origin(base) != context.root:
            context.note(f"{introduction} names no same-origin things listing")
            return None
        context.note(f"Detected a WoT Thing Description Directory at {base}")
        return SourceDefinition(
            id=base,
            external_id=base,
            provider=self.name,
            config={"url": base},
            title=(text(document.get("title")) or base)[:300],
            description=text(document.get("description"))[:1000],
            tags=("external source", "web of things"),
        )

    async def _get(
        self,
        source: SourceDefinition,
        url: str,
        *,
        public_http: BoundedHttpClient | None,
    ) -> HttpPayload:
        client = public_http or source_client(source, max_bytes=_MAX_BYTES)
        secrets = credential_headers(source)
        response = await client.get(
            url,
            headers={"Accept": _ACCEPT, **secrets},
            max_bytes=_MAX_BYTES,
            credentialed=bool(secrets),
        )
        if response.status in {401, 403}:
            raise SourceAuthenticationError(f"Directory '{source.id}' rejected its credential")
        return response

    @staticmethod
    def _json(source: SourceDefinition, response: HttpPayload) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SourceProtocolError(f"Directory '{source.id}' returned invalid JSON") from exc

    async def _things(
        self,
        source: SourceDefinition,
        *,
        public_http: BoundedHttpClient | None,
    ) -> list[dict[str, Any]]:
        base = str(source.get("url"))
        url: str | None = f"{base}/things?{urlencode({'offset': 0, 'limit': _PAGE_SIZE})}"
        things: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            response = await self._get(source, url, public_http=public_http)
            if not 200 <= response.status < 300:
                raise SourceUnavailableError(f"Directory listing returned HTTP {response.status}")
            page = self._json(source, response)
            # Early drafts of the spec wrapped the listing in a collection object.
            if isinstance(page, dict) and isinstance(page.get("members"), list):
                page = page["members"]
            if not isinstance(page, list):
                raise SourceProtocolError("Directory listing must be an array of TDs")
            for td in page:
                if not isinstance(td, dict):
                    raise SourceProtocolError("Directory listing entry must be a TD object")
                thing_id = td.get("id")
                if not isinstance(thing_id, str) or not thing_id.strip():
                    continue
                if thing_id in seen:
                    raise SourceProtocolError("Directory listing contains duplicate identities")
                seen.add(thing_id)
                things.append(td)
            if len(things) > _MAX_THINGS:
                raise SourceProtocolError(
                    f"Directory lists more than {_MAX_THINGS} Things; too large to rank here"
                )
            url = next_link(response)
            if url and origin_of(url) != origin_of(base):
                raise SourceProtocolError("Directory pagination left the directory origin")
        return things

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        base = str(source.get("url"))
        candidates = [
            (
                CandidateDraft(
                    provider=self.name,
                    source_id=source.id,
                    external_id=td["id"],
                    kind="thing-description",
                    title=(text(td.get("title")) or td["id"])[:500],
                    summary=text(td.get("description"))[:2000],
                    links=(
                        {
                            "title": "Thing Description",
                            "url": f"{base}/things/{quote(td['id'], safe='')}",
                            "media_type": "application/td+json",
                        },
                    ),
                ),
                search_text(td),
            )
            for td in await self._things(source, public_http=public_http)
        ]
        return rank_candidates(intent, candidates, limit=limit, require_match=False)

    async def onboarding_document(
        self,
        source: SourceDefinition,
        candidate: CandidateDraft,
        *,
        runtime: OnboardingRuntime,
    ) -> OnboardingResult:
        del runtime
        url = f"{source.get('url')}/things/{quote(candidate.external_id, safe='')}"
        response = await self._get(source, url, public_http=None)
        if response.status in {404, 410}:
            raise StaleCandidateError("The directory no longer lists this Thing; discover it again")
        if not 200 <= response.status < 300:
            raise SourceUnavailableError(f"Directory returned HTTP {response.status}")
        document = self._json(source, response)
        if not isinstance(document, dict) or document.get("id") != candidate.external_id:
            raise SourceProtocolError("TD identity does not match its directory listing")
        document.pop("registration", None)
        return OnboardingResult(document=document)
