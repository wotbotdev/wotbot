"""OGC SensorThings API: sensor Things, their Datastreams, and Observation history.

Implements the client side of OGC SensorThings API Part 1: Sensing, versions
1.0 and 1.1 (https://docs.ogc.org/is/18-088/18-088.html). Only the read side
is used: reading over HTTP and receiving updates over MQTT. The
create-update-delete and batch extensions are not.

Configuration
    ``url``
        The service root, such as ``https://iot.hamburg.de/v1.1``, under which
        ``/Things`` and ``/Datastreams`` are served. Required.

Search filters on the server, because a city-scale service lists far more
Things than can be fetched and ranked here (Hamburg serves over 9,000). Every
search term must occur in the Thing's ``name`` or ``description``, compared
case-insensitively with the spec's built-in ``substringof`` and ``tolower``;
when no Thing matches every term, a Thing matching any term qualifies. Newer
Things come first (``$orderby=@iot.id desc``), since long-running services keep
superseded entities around. At most ``_SEARCH_WINDOW`` Things are fetched and
then ranked here.

Onboarding turns one STA Thing into one WoT Thing. Each of its first
``_MAX_DATASTREAMS`` Datastreams contributes

* a property reading the latest Observation straight from the service, as a
  plain HTTP form with a fixed query. It is only generated for ``nosec``
  sources, because the runtime has no credential for a direct form; and
* a ``<name>_history`` action, dispatched through this provider, that turns
  ``from``, ``to``, and ``limit`` into an OData query, pages through
  ``@iot.nextLink`` within fixed bounds, and returns the Observations in
  chronological order. OData's ``$``-prefixed parameter names are not valid
  URI-template variables, which is why history cannot be a direct form; and
* a ``<name>_observation`` event subscribing to the Datastream's
  ``v1.x/Datastreams(<id>)/Observations`` MQTT topic, when the service root
  advertises a receive-updates MQTT endpoint. The broker must be on the
  service's own host, so a service cannot point the runtime at another
  machine, and the topic travels in the href path because that is where
  node-wot's MQTT binding reads it. Like the property, it is ``nosec`` only.

MultiDatastreams are not modeled. There is no refresh.

Detection fetches the service root document and accepts it when it lists
``Things`` and ``Datastreams`` collections at the same origin. The root is the
URL's ``/v1.x`` prefix when the path has one; for a bare origin, ``/v1.1`` and
then ``/v1.0`` are tried. It runs before OpenAPI, because FROST servers also
publish an OpenAPI document that would otherwise win.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from wotbot.discovery.detection import DetectionContext, origin
from wotbot.discovery.errors import (
    ProviderError,
    SourceAuthenticationError,
    SourceProtocolError,
    SourceUnavailableError,
    StaleCandidateError,
)
from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.models import (
    CandidateDraft,
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
    credential_headers,
    is_http_endpoint,
    provider_action_href,
    provider_thing_id,
    source_client,
    text,
)
from wotbot.discovery.search import rank_candidates

PROVIDER_NAME = "sensorthings"
_MAX_BYTES = 4 * 1024 * 1024
_SEARCH_WINDOW = 100
_SUMMARY_DATASTREAMS = 20
_MAX_SEARCH_TERMS = 6
_MAX_DATASTREAMS = 50
_HISTORY_DEFAULT_LIMIT = 1000
_HISTORY_MAX_LIMIT = 5000
_HISTORY_PAGE_SIZE = 1000
_HISTORY_MAX_PAGES = 10
_HISTORY_VARIABLES = ("from", "to", "limit")
_MAX_ATTRIBUTE_BYTES = 4096
# Characters OData expressions use that need no escaping in a query value.
_ODATA_SAFE = "(),:;'@$=*/"
# An entity key is a number or an OData string literal with doubled quotes.
_KEY = r"(?:\d+|'(?:[^']|'')*')"
# The same key once percent-encoded for a path: no raw separators remain.
_PATH_KEY = r"(?:\d+|'(?:[^'/?#\\]|'')*')"
_HISTORY_PATH = re.compile(rf"/Datastreams\({_PATH_KEY}\)/Observations")
_MQTT_UPDATES = "receive-updates-via-mqtt/receive-updates"
_MQTT_CONTEXT = {"mqv": "https://www.w3.org/2019/wot/mqtt#"}
# What a topic keeps unescaped in an mqtt:// URL path, without the + and #
# wildcards, so the subscribed filter is exactly the Datastream's topic.
_MQTT_TOPIC = re.compile(r"[A-Za-z0-9._~()'!*$&,;=:@/-]+")
_VERSION_ROOT = re.compile(r"^(.*?/v1\.[01])(?:/|$)")


def entity_key(value: Any) -> str:
    """Render an ``@iot.id`` as the key literal used in a resource path."""

    if isinstance(value, bool) or value is None:
        raise SourceProtocolError("SensorThings entity has no usable @iot.id")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value and len(value) <= 200:
        return odata_string(value)
    raise SourceProtocolError("SensorThings entity has no usable @iot.id")


def key_segment(key: str) -> str:
    """Percent-encode a key literal for a resource path, keeping its quotes."""

    return quote(key, safe="'")


def odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def query_string(params: dict[str, str]) -> str:
    """Encode OData system options, keeping their ``$`` names literal."""

    return "&".join(f"{name}={quote(value, safe=_ODATA_SAFE)}" for name, value in params.items())


def rebased_next_link(next_link: Any, base: str) -> str:
    """Follow ``@iot.nextLink`` through the registered root, not the server's spelling.

    Behind a proxy a server may name itself ``http://`` or by an internal host.
    Only the path and query are taken from the link, and the path must stay
    beneath the root, so pagination never reaches another host or service.
    """

    if not isinstance(next_link, str) or len(next_link) > 8192:
        raise SourceProtocolError("SensorThings pagination link is invalid")
    link = urlparse(next_link)
    root = urlparse(base)
    if not link.path.startswith(root.path.rstrip("/") + "/") or link.fragment:
        raise SourceProtocolError("SensorThings pagination left the service root")
    return origin(base) + link.path + (f"?{link.query}" if link.query else "")


def search_filter(terms: list[str], *, require_all: bool) -> str:
    clauses = [
        "("
        f"substringof({odata_string(term)},tolower(name)) or "
        f"substringof({odata_string(term)},tolower(description))"
        ")"
        for term in terms
    ]
    return (" and " if require_all else " or ").join(clauses)


def search_terms(intent: SearchIntent) -> list[str]:
    terms: list[str] = []
    for term in intent.terms:
        folded = " ".join(term.lower().split())[:80]
        if folded and folded not in terms:
            terms.append(folded)
    return terms[:_MAX_SEARCH_TERMS]


def service_root(document: Any, document_url: str) -> str | None:
    """Return the service root a root document describes, if it is one."""

    if not isinstance(document, dict) or not isinstance(document.get("value"), list):
        return None
    collections = {
        str(item.get("name")): str(item.get("url") or "")
        for item in document["value"]
        if isinstance(item, dict)
    }
    things = collections.get("Things", "").rstrip("/")
    if not things.endswith("/Things") or "Datastreams" not in collections:
        return None
    base = things[: -len("/Things")]
    if not is_http_endpoint(base) or origin(base) != origin(document_url):
        return None
    return base


def mqtt_broker(root: Any, base: str) -> str | None:
    """Return the receive-updates MQTT broker a service root names on its own host."""

    settings = root.get("serverSettings") if isinstance(root, dict) else None
    if not isinstance(settings, dict):
        return None
    host = urlparse(base).hostname
    for name, value in settings.items():
        if not str(name).endswith(_MQTT_UPDATES) or not isinstance(value, dict):
            continue
        endpoints = value.get("endpoints")
        for endpoint in endpoints if isinstance(endpoints, list) else ():
            if not isinstance(endpoint, str):
                continue
            parsed = urlparse(endpoint.strip())
            try:
                port = parsed.port
            except ValueError:
                continue
            if (
                parsed.scheme in {"mqtt", "mqtts"}
                and host
                and parsed.hostname == host
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                literal = f"[{host}]" if ":" in host else host
                return f"{parsed.scheme}://{literal}" + (f":{port}" if port else "")
    return None


def property_slug(datastream_name: str, thing_name: str) -> str:
    """Name an affordance after its Datastream, minus the Thing's own name."""

    label = datastream_name
    match = re.search(re.escape(thing_name), label, re.IGNORECASE) if thing_name else None
    if match:
        shortened = label[: match.start()] + label[match.end() :]
        if re.search(r"[^\W_]", shortened):
            label = shortened
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", ascii_label).strip("_")[:60].rstrip("_") or "datastream"


def parse_time(name: str, value: Any, *, end_of_day: bool = False) -> str | None:
    """Normalize an ISO 8601 bound to UTC; a bare date as ``to`` means its last instant."""

    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"'{name}' must be an ISO 8601 date or date-time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"'{name}' must be an ISO 8601 date or date-time") from exc
    if end_of_day and re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", value.strip()):
        parsed += timedelta(days=1, milliseconds=-1)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_limit(value: Any) -> int:
    if value is None or value == "":
        return _HISTORY_DEFAULT_LIMIT
    if isinstance(value, int) and not isinstance(value, bool):
        limit = value
    elif isinstance(value, str) and re.fullmatch(r"\s*\d{1,9}\s*", value):
        limit = int(value)
    else:
        raise ValueError("'limit' must be an integer")
    if not 1 <= limit <= _HISTORY_MAX_LIMIT:
        raise ValueError(f"'limit' must be between 1 and {_HISTORY_MAX_LIMIT}")
    return limit


def _unit(datastream: dict[str, Any]) -> str:
    unit = datastream.get("unitOfMeasurement")
    if not isinstance(unit, dict):
        return ""
    symbol = text(unit.get("symbol")).strip()
    name = text(unit.get("name")).strip()
    if symbol and name and symbol != name:
        return f"{name} ({symbol})"
    return symbol or name


def _observation_schema(datastream: dict[str, Any]) -> dict[str, Any]:
    observed = datastream.get("ObservedProperty")
    observed = observed if isinstance(observed, dict) else {}
    result: dict[str, Any] = {
        "description": text(observed.get("name")) or "Observed value",
    }
    if unit := _unit(datastream):
        result["unit"] = unit
    return {
        "type": "object",
        "properties": {
            "phenomenonTime": {
                "type": "string",
                "description": "When the value was observed: an instant or an ISO 8601 interval",
            },
            "result": result,
        },
    }


class SensorThingsProvider(DiscoveryProvider):
    name = PROVIDER_NAME
    capabilities = ("detect", "search", "onboard")
    # The root document is recognized exactly, and FROST servers also publish
    # an OpenAPI document that the OpenAPI probe (priority 10) would accept.
    detect_priority = 5
    public_max_bytes = _MAX_BYTES
    public_max_requests = _HISTORY_MAX_PAGES + 2
    config = ProviderConfigSpec(
        fields=frozenset({"url"}),
        url_fields=("url",),
        title="OGC SensorThings API",
    )

    async def inspect_public(self, context: DetectionContext) -> SourceDefinition | None:
        parsed = urlparse(context.url)
        if match := _VERSION_ROOT.match(parsed.path):
            candidates = [context.root + match.group(1)]
        elif parsed.path in {"", "/"}:
            candidates = [f"{context.root}/v1.1", f"{context.root}/v1.0"]
        else:
            context.note("No SensorThings version segment (/v1.0 or /v1.1) in the URL")
            return None
        for url in candidates:
            payload = await context.http.get(url, headers={"Accept": "application/json"})
            if not 200 <= payload.status < 300:
                context.note(f"No SensorThings service root at {url} (HTTP {payload.status})")
                continue
            try:
                document = payload.json()
            except ValueError:
                context.note(f"{url} is not a SensorThings service root")
                continue
            base = service_root(document, payload.url)
            if base is None:
                context.note(f"{url} is not a SensorThings service root")
                continue
            version = base.rsplit("/", 1)[-1]
            host = urlparse(base).hostname or base
            context.note(f"Detected a SensorThings API {version} service at {base}")
            return SourceDefinition(
                id=base,
                external_id=base,
                provider=self.name,
                config={"url": base},
                title=f"SensorThings API {host}"[:300],
                description=(
                    f"OGC SensorThings API {version} service with sensor Things, "
                    "Datastreams, and Observations"
                ),
                tags=("external source", "sensorthings", "sensor data"),
            )
        return None

    async def _get_json(
        self,
        source: SourceDefinition,
        url: str,
        *,
        client: BoundedHttpClient,
    ) -> Any:
        """Fetch one JSON document; ``None`` means the entity no longer exists."""

        secrets = credential_headers(source)
        response = await client.get(
            url,
            headers={"Accept": "application/json", **secrets},
            max_bytes=_MAX_BYTES,
            credentialed=bool(secrets),
        )
        if response.status in {401, 403}:
            raise SourceAuthenticationError(
                f"SensorThings service '{source.id}' rejected its credential"
            )
        if response.status in {404, 410}:
            return None
        if not 200 <= response.status < 300:
            raise SourceUnavailableError(
                f"SensorThings service '{source.id}' returned HTTP {response.status}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceProtocolError(
                f"SensorThings service '{source.id}' returned invalid JSON"
            ) from exc

    async def _find_things(
        self,
        source: SourceDefinition,
        filter_expression: str,
        *,
        client: BoundedHttpClient,
    ) -> list[dict[str, Any]]:
        params = {
            "$top": str(_SEARCH_WINDOW),
            "$orderby": "@iot.id desc",
            "$select": "@iot.id,name,description,properties",
            "$expand": f"Datastreams($select=name;$top={_SUMMARY_DATASTREAMS + 1})",
        }
        if filter_expression:
            params["$filter"] = filter_expression
        url = f"{source.get('url')}/Things?{query_string(params)}"
        page = await self._get_json(source, url, client=client)
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise SourceProtocolError("SensorThings Things listing must be a 'value' array")
        return [item for item in page["value"] if isinstance(item, dict)]

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        client = public_http or source_client(source, max_bytes=_MAX_BYTES)
        terms = search_terms(intent)
        things = await self._find_things(
            source, search_filter(terms, require_all=True) if terms else "", client=client
        )
        if not things and len(terms) > 1:
            things = await self._find_things(
                source, search_filter(terms, require_all=False), client=client
            )
        base = str(source.get("url"))
        candidates: list[tuple[CandidateDraft, str]] = []
        for thing in things:
            try:
                key = entity_key(thing.get("@iot.id"))
            except SourceProtocolError:
                continue
            name = text(thing.get("name")) or f"Thing {key}"
            description = text(thing.get("description"))
            datastreams = [
                text(item.get("name"))
                for item in thing.get("Datastreams") or ()
                if isinstance(item, dict) and text(item.get("name"))
            ]
            properties = thing.get("properties")
            keywords = (
                [item for item in properties.get("keywords") or () if isinstance(item, str)]
                if isinstance(properties, dict) and isinstance(properties.get("keywords"), list)
                else []
            )
            summary = description
            if datastreams:
                listed = "; ".join(datastreams[:5])
                # One past the cap was fetched, so a capped count is a true lower bound.
                others = len(datastreams) - 5
                bound = "at least " if len(datastreams) > _SUMMARY_DATASTREAMS else ""
                more = f" (+{bound}{others} more)" if others > 0 else ""
                summary = f"{description} — Datastreams: {listed}{more}".strip(" —")
            candidates.append(
                (
                    CandidateDraft(
                        provider=self.name,
                        source_id=source.id,
                        external_id=key,
                        kind="sensor-thing",
                        title=name[:500],
                        summary=summary[:2000],
                        links=(
                            {
                                "title": "SensorThings entity",
                                "url": f"{base}/Things({key_segment(key)})",
                                "media_type": "application/json",
                            },
                        ),
                    ),
                    " ".join([name, description, *keywords, *datastreams]),
                )
            )
        return rank_candidates(intent, candidates, limit=limit, require_match=False)

    async def onboarding_document(
        self,
        source: SourceDefinition,
        candidate: CandidateDraft,
        *,
        runtime: OnboardingRuntime,
    ) -> OnboardingResult:
        del runtime
        key = candidate.external_id
        if not re.fullmatch(_KEY, key):
            raise SourceProtocolError("Candidate does not name a SensorThings entity")
        base = str(source.get("url"))
        params = {
            "$select": "@iot.id,name,description,properties",
            "$expand": (
                "Datastreams("
                "$select=@iot.id,name,description,unitOfMeasurement,observationType,"
                "phenomenonTime;"
                "$expand=ObservedProperty($select=name,definition,description);"
                f"$orderby=@iot.id asc;$top={_MAX_DATASTREAMS + 1}"
                "),Locations($select=location;$top=1)"
            ),
        }
        url = f"{base}/Things({key_segment(key)})?{query_string(params)}"
        client = source_client(source, max_bytes=_MAX_BYTES)
        thing = await self._get_json(source, url, client=client)
        if thing is None:
            raise StaleCandidateError(
                "The SensorThings service no longer serves this Thing; discover it again"
            )
        if not isinstance(thing, dict) or entity_key(thing.get("@iot.id")) != key:
            raise SourceProtocolError("SensorThings Thing identity does not match its listing")
        direct_reads = source.security_scheme == "nosec"
        broker = None
        if direct_reads:
            try:
                broker = mqtt_broker(await self._get_json(source, base, client=client), base)
            except ProviderError:
                broker = None
        return thing_document(
            thing,
            base=base,
            thing_id=provider_thing_id(self.name, candidate.source_id, key),
            direct_reads=direct_reads,
            mqtt_broker=broker,
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
        del external_id, path_variables, query_variables
        if method != "GET" or not _HISTORY_PATH.fullmatch(path):
            raise ValueError("SensorThings actions only read Datastream Observations")
        variables = {
            name: value
            for name, value in (input_data.items() if isinstance(input_data, dict) else ())
            if name in _HISTORY_VARIABLES
        }
        variables.update(uri_variables)
        start = parse_time("from", variables.get("from"))
        end = parse_time("to", variables.get("to"), end_of_day=True)
        if start and end and start > end:
            raise ValueError("'from' must not be later than 'to'")
        limit = parse_limit(variables.get("limit"))

        base = str(source.get("url"))
        params = {
            "$select": "phenomenonTime,result",
            "$orderby": "phenomenonTime desc",
            "$top": str(min(limit, _HISTORY_PAGE_SIZE)),
        }
        bounds = [
            clause
            for clause in (
                f"phenomenonTime ge {start}" if start else "",
                f"phenomenonTime le {end}" if end else "",
            )
            if clause
        ]
        if bounds:
            params["$filter"] = " and ".join(bounds)
        url: str | None = f"{base}{path}?{query_string(params)}"
        client = public_http or source_client(
            source, max_bytes=_MAX_BYTES, max_requests=_HISTORY_MAX_PAGES + 2, timeout_seconds=30
        )
        observations: list[dict[str, Any]] = []
        truncated = False
        pages = 0
        while url:
            if pages == _HISTORY_MAX_PAGES:
                truncated = True
                break
            pages += 1
            page = await self._get_json(source, url, client=client)
            if page is None:
                raise SourceUnavailableError(
                    "The SensorThings service no longer serves this Datastream"
                )
            if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                raise SourceProtocolError("SensorThings Observations must be a 'value' array")
            for item in page["value"]:
                if isinstance(item, dict):
                    observations.append(
                        {
                            "phenomenonTime": item.get("phenomenonTime"),
                            "result": item.get("result"),
                        }
                    )
            next_link = page.get("@iot.nextLink")
            if len(observations) >= limit:
                truncated = len(observations) > limit or bool(next_link)
                break
            url = None if next_link is None else rebased_next_link(next_link, base)
        observations = observations[:limit]
        observations.reverse()
        body: dict[str, Any] = {
            **({"from": start} if start else {}),
            **({"to": end} if end else {}),
            "count": len(observations),
            "truncated": truncated,
            "observations": observations,
        }
        return ProviderResponse(
            body=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
            content_type="application/json",
        )


def thing_document(
    thing: dict[str, Any],
    *,
    base: str,
    thing_id: str,
    direct_reads: bool,
    mqtt_broker: str | None = None,
) -> OnboardingResult:
    """Compile one STA Thing, with its Datastreams expanded, into a TD."""

    key = entity_key(thing.get("@iot.id"))
    version = base.rstrip("/").rsplit("/", 1)[-1]
    if not direct_reads or not re.fullmatch(r"v1\.[01]", version):
        mqtt_broker = None
    thing_name = text(thing.get("name")) or f"Thing {key}"
    raw_datastreams = [item for item in thing.get("Datastreams") or () if isinstance(item, dict)]
    warnings: list[str] = []
    if len(raw_datastreams) > _MAX_DATASTREAMS:
        warnings.append(f"Generated the first {_MAX_DATASTREAMS} Datastreams of this Thing")
        raw_datastreams = raw_datastreams[:_MAX_DATASTREAMS]
    if not raw_datastreams:
        warnings.append("This Thing has no Datastreams; it has no readable values")
    if raw_datastreams and not direct_reads:
        warnings.append(
            "The source needs a credential, so latest values are only available "
            "through the history actions (limit=1)"
        )

    properties: dict[str, Any] = {}
    actions: dict[str, Any] = {}
    events: dict[str, Any] = {}
    used: set[str] = set()
    for datastream in raw_datastreams:
        try:
            datastream_key = entity_key(datastream.get("@iot.id"))
        except SourceProtocolError:
            warnings.append("Skipped a Datastream without a usable @iot.id")
            continue
        datastream_name = text(datastream.get("name")) or f"Datastream {datastream_key}"
        slug = base_slug = property_slug(datastream_name, thing_name)
        if slug in used:
            slug = base_slug = (
                f"{slug}_{re.sub(r'[^A-Za-z0-9]+', '_', datastream_key).strip('_')}".rstrip("_")
            )
        counter = 2
        while slug in used:
            slug = f"{base_slug}_{counter}"
            counter += 1
        used.add(slug)
        description_parts = [text(datastream.get("description"))]
        if unit := _unit(datastream):
            description_parts.append(f"Unit: {unit}.")
        if period := text(datastream.get("phenomenonTime")):
            description_parts.append(f"Observed period: {period}.")
        description = " ".join(part.strip() for part in description_parts if part.strip())
        observation = _observation_schema(datastream)
        observations_path = f"/Datastreams({key_segment(datastream_key)})/Observations"

        if direct_reads:
            latest_query = query_string(
                {
                    "$top": "1",
                    "$orderby": "phenomenonTime desc",
                    "$select": "phenomenonTime,result",
                }
            )
            properties[slug] = {
                "title": datastream_name[:500],
                "description": f"Latest observation. {description}".strip(),
                "readOnly": True,
                "type": "object",
                "properties": {
                    "value": {
                        "type": "array",
                        "description": "The latest observation, or none yet",
                        "items": observation,
                    }
                },
                "wotbot:generatedBy": PROVIDER_NAME,
                "forms": [
                    {
                        "href": f"{base}{observations_path}?{latest_query}",
                        "op": ["readproperty"],
                        "htv:methodName": "GET",
                        "contentType": "application/json",
                    }
                ],
            }

        action_name = f"{slug}_history"
        actions[action_name] = {
            "title": f"History: {datastream_name}"[:500],
            "description": (
                "Observations of this Datastream in chronological order. "
                "'from' and 'to' bound the observation time (ISO 8601, inclusive; "
                "UTC unless an offset is given). When more than 'limit' observations "
                "match, the most recent ones are returned and 'truncated' is true. " + description
            ).strip(),
            "safe": True,
            "idempotent": True,
            "wotbot:generatedBy": PROVIDER_NAME,
            "uriVariables": {
                "from": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Earliest observation time, inclusive",
                },
                "to": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Latest observation time, inclusive; a date means its end",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _HISTORY_MAX_LIMIT,
                    "default": _HISTORY_DEFAULT_LIMIT,
                    "description": "Maximum number of observations to return",
                },
            },
            "output": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Absent when unbounded"},
                    "to": {"type": "string", "description": "Absent when unbounded"},
                    "count": {"type": "integer"},
                    "truncated": {
                        "type": "boolean",
                        "description": "Older observations in the window were left out",
                    },
                    "observations": {"type": "array", "items": observation},
                },
            },
            "forms": [
                {
                    "href": provider_action_href(thing_id, action_name)
                    + "{?"
                    + ",".join(_HISTORY_VARIABLES)
                    + "}",
                    "op": ["invokeaction"],
                    "htv:methodName": "GET",
                    "contentType": "application/json",
                    "security": ["nosec_sc"],
                    "response": {"contentType": "application/json"},
                    "wotbot:providerOperation": "invoke",
                    "wotbot:httpMethod": "GET",
                    "wotbot:path": observations_path,
                    "wotbot:pathVariables": [],
                    "wotbot:queryVariables": list(_HISTORY_VARIABLES),
                }
            ],
        }

        topic = f"{version}/Datastreams({datastream_key})/Observations"
        if mqtt_broker and _MQTT_TOPIC.fullmatch(topic):
            events[f"{slug}_observation"] = {
                "title": f"New observation: {datastream_name}"[:500],
                "description": (
                    "Pushed over MQTT each time the service stores a new Observation "
                    "of this Datastream; the data is the full Observation entity. " + description
                ).strip(),
                "data": observation,
                "wotbot:generatedBy": PROVIDER_NAME,
                "forms": [
                    {
                        "href": f"{mqtt_broker}/{topic}",
                        "op": ["subscribeevent", "unsubscribeevent"],
                        "contentType": "application/json",
                        "mqv:filter": topic,
                    }
                ],
            }
        elif mqtt_broker:
            warnings.append(f"Datastream {datastream_key} has no usable MQTT topic")

    document: dict[str, Any] = {
        "@context": [TD_CONTEXT, WOTBOT_CONTEXT, *([_MQTT_CONTEXT] if events else [])],
        "id": thing_id,
        "title": thing_name[:500],
        "description": text(thing.get("description"))[:2000],
        "security": ["nosec_sc"],
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "links": [
            {
                "href": f"{base}/Things({key_segment(key)})",
                "rel": "alternate",
                "type": "application/json",
            }
        ],
    }
    locations = [item for item in thing.get("Locations") or () if isinstance(item, dict)]
    location = locations[0].get("location") if locations else None
    if isinstance(location, dict) and location.get("type") == "Feature":
        location = location.get("geometry")
    if isinstance(location, dict) and isinstance(location.get("type"), str):
        document["wotbot:location"] = location
    # Hamburg, for one, records a counter's direction and owner only here.
    attributes = thing.get("properties")
    if isinstance(attributes, dict) and attributes:
        encoded = json.dumps(attributes, ensure_ascii=False, default=str)
        if len(encoded) <= _MAX_ATTRIBUTE_BYTES:
            document["wotbot:sensorThingsProperties"] = json.loads(encoded)
    if properties:
        document["properties"] = properties
    if actions:
        document["actions"] = actions
    if events:
        document["events"] = events
    return OnboardingResult(document=document, warnings=tuple(warnings))
