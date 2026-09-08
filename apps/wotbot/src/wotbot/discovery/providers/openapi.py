"""OpenAPI: HTTP operations compiled from a specification into TD actions.

Expects OpenAPI 3.0, OpenAPI 3.1, or Swagger 2.0, as JSON or YAML, at the
configured URL. This is the only provider that authors a full Thing Description
itself, so most of this module is the compiler and its limits.

Detection also accepts a documentation page. Swagger UI and ReDoc name their
specification in the HTML they serve, and ``service-desc`` is the registered
link relation for it, so a docs URL is followed once to the document it
declares -- same-origin only. The source's identity stays the specification,
which is what refresh must be able to re-fetch.
An API base URL can also be detected through ``openapi.json`` or ``swagger.json``
beneath it, using at most two additional probes within the shared request budget.

Configuration
    ``url``
        The specification document. Required. The final URL after redirects
        becomes the source's immutable identity, so a spec that later redirects
        elsewhere is refused rather than silently followed.
    ``server_url_override``
        Use this API base instead of the one the document declares. Optional,
        for specs published with a placeholder or internal server.
    ``api_key_header``
        Header carrying the API key. Defaults to ``X-Api-Key``.

What is compiled, and what is skipped
    Only ``$ref`` pointers within the document resolve; external references are
    unsupported. Only one security scheme is used per Thing — an API key in a
    header, HTTP basic, or HTTP bearer — and compound requirements are
    unsupported, so operations needing anything else are dropped with a
    warning rather than silently exposed unauthenticated. Request and response
    bodies must be JSON. Path and query parameters must be primitive; a
    required header, cookie, or form parameter drops its operation, while an
    optional one is ignored with a warning.

Bounds, because the document is untrusted input
    4 MiB of spec, 100k nodes, depth 100, 2000 operations, schema depth 20 and
    2000 nodes, and a generated TD of 512 KiB.

A spec with more than 30 usable operations is split into groups by tag,
chunked, so one Thing never carries an unusable number of actions; at or below
that it becomes a single ``all`` group. The chosen group, the spec digest, and
the compiler version are recorded under ``wotbot:generation``, which is what
makes this the one provider supporting refresh: regeneration reproduces exactly
the same group, and every node it authors is stamped so a person's hand-written
additions survive the merge. Onboarding refuses a candidate whose digest no
longer matches, so a spec that changed between search and onboarding is
re-discovered instead of compiled from stale metadata.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml

from wotbot.discovery.detection import DetectionContext
from wotbot.discovery.errors import (
    ProviderError,
    SourceAuthenticationError,
    SourceProtocolError,
    StaleCandidateError,
)
from wotbot.discovery.http import BoundedHttpClient, HttpPayload, origin_of, resolve_public
from wotbot.discovery.models import (
    CandidateDraft,
    OnboardingResult,
    ProviderConfigSpec,
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
)
from wotbot.discovery.search import rank_candidates

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
_MAX_SPEC_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENT_NODES = 100_000
_MAX_DOCUMENT_DEPTH = 100
_MAX_OPERATIONS = 2_000
_MAX_GROUP_OPERATIONS = 30
_MAX_TD_BYTES = 512 * 1024
_MAX_SCHEMA_DEPTH = 20
_MAX_SCHEMA_NODES = 2_000
_COMPILER_VERSION = 3
_JSON_MEDIA_TYPES = {"application/json", "text/json"}
_NAME = re.compile(r"[^A-Za-z0-9_]+")
_URI_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class OpenApiError(SourceProtocolError):
    """An OpenAPI document could not be compiled into a Thing Description."""


@dataclass(frozen=True, slots=True)
class SecurityChoice:
    source_name: str
    td_name: str
    definition: dict[str, Any]
    supported: bool = True


@dataclass(frozen=True, slots=True)
class Operation:
    key: str
    method: str
    path: str
    operation_id: str
    title: str
    description: str
    tag: str
    parameters: tuple[dict[str, Any], ...]
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    response_media_type: str | None
    security_name: str


@dataclass(frozen=True, slots=True)
class ParsedApi:
    document: dict[str, Any]
    digest: str
    spec_url: str
    version: str
    title: str
    description: str
    server_url: str
    security: SecurityChoice
    security_definitions: dict[str, dict[str, Any]]
    operations: tuple[Operation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperationGroup:
    key: str
    label: str
    operations: tuple[Operation, ...]


class OpenApiProvider(DiscoveryProvider):
    name = "openapi"
    capabilities = ("detect", "search", "onboard", "refresh")
    detect_priority = 10
    public_max_bytes = _MAX_SPEC_BYTES
    config = ProviderConfigSpec(
        fields=frozenset({"url", "server_url_override", "api_key_header"}),
        url_fields=("url",),
        optional_url_fields=("server_url_override",),
        text_defaults=(("api_key_header", "X-Api-Key"),),
        title="OpenAPI specification",
    )

    def normalize_config(self, config: dict[str, Any]) -> dict[str, Any]:
        specification_url = str(config.get("url") or "").strip()
        normalized = super().normalize_config(config)
        if not is_http_endpoint(specification_url):
            raise ValueError("Provider requires a valid 'url' URL")
        # A trailing slash can be part of a direct specification endpoint. Keep
        # the exact final redirect URL as the source's immutable identity.
        normalized["url"] = specification_url
        return normalized

    async def inspect_public(self, context: DetectionContext) -> SourceDefinition | None:
        found = await self._probe_specification(context, context.url, allow_follow=True)
        if found is None:
            return None
        document, version, server, spec_url = found
        info = document.get("info") if isinstance(document.get("info"), dict) else {}
        context.note(f"Detected {version} with API server {server}")
        return SourceDefinition(
            id=spec_url,
            external_id=spec_url,
            provider=self.name,
            title=_bounded_text(info.get("title"), 500) or "OpenAPI service",
            description=_bounded_text(info.get("description"), 2_000),
            tags=("OpenAPI", version, "external source"),
            config={"url": spec_url},
        )

    async def _probe_specification(
        self,
        context: DetectionContext,
        url: str,
        *,
        allow_follow: bool,
    ) -> tuple[dict[str, Any], str, str, str] | None:
        """Read a spec, then try a declared link or two conventional base paths.

        Only the initial response allows discovery; subsequent probes must be
        specifications themselves. Every request uses the same bounded client.
        """

        try:
            response = await context.http.get(
                url,
                headers={
                    "Accept": (
                        "application/vnd.oai.openapi+json,application/json,"
                        "application/yaml,text/yaml,application/x-yaml,text/html;q=0.1"
                    )
                },
                max_bytes=_MAX_SPEC_BYTES,
            )
        except ProviderError as exc:
            context.note(f"OpenAPI probe failed: {exc}")
            return None
        context.note(
            f"Fetched {response.url} (HTTP {response.status}, {response.content_type or 'unknown'})"
        )
        if 200 <= response.status < 300:
            try:
                document = parse_openapi(response.body)
                version = openapi_version(document)
                server = resolve_server(document, response.url, "")
                await resolve_public(server)
            except (OpenApiError, ProviderError, ValueError, yaml.YAMLError) as exc:
                context.note(f"Not an OpenAPI document: {exc}")
            else:
                return document, version, server, response.url
            declared = declared_spec_url(response) if allow_follow else None
            if declared is not None:
                context.note(f"{response.url} declares its API description at {declared}")
                return await self._probe_specification(context, declared, allow_follow=False)
        elif response.status not in {404, 405}:
            return None

        if not allow_follow:
            return None
        parsed = urlparse(response.url)
        path = parsed.path.rstrip("/")
        if path.lower().endswith(
            (".json", ".yaml", ".yml", ".html", ".htm", ".rdf", ".ttl", ".xml")
        ):
            return None
        # Keep an API prefix such as /api/v1, including after redirects, while
        # discarding query parameters that belong only to the supplied URL.
        base = parsed._replace(path=f"{path}/", params="", query="", fragment="").geturl()
        for filename in ("openapi.json", "swagger.json"):
            spec_url = urljoin(base, filename)
            context.note(f"Trying conventional OpenAPI specification at {spec_url}")
            found = await self._probe_specification(context, spec_url, allow_follow=False)
            if found is not None:
                return found
        return None

    async def search(
        self,
        source: SourceDefinition,
        intent: SearchIntent,
        limit: int,
        *,
        public_http: BoundedHttpClient | None = None,
    ) -> list[CandidateDraft]:
        parsed = await self._load(source, public_http=public_http)
        candidates: list[tuple[CandidateDraft, str]] = []
        for group in operation_groups(parsed.operations):
            external_id = group_external_id(
                source.external_id or source.id,
                parsed.server_url,
                group.key,
            )
            methods = sorted({operation.method.upper() for operation in group.operations})
            summaries = " ".join(
                f"{operation.operation_id} {operation.method} {operation.path} "
                f"{operation.title} {operation.description}"
                for operation in group.operations
            )
            label = parsed.title if group.key == "all" else f"{parsed.title} — {group.label}"
            summary = (
                f"{len(group.operations)} supported operations on {parsed.server_url}. "
                f"Methods: {', '.join(methods)}."
            )
            candidate = CandidateDraft(
                provider=self.name,
                source_id=source.id,
                external_id=external_id,
                kind="api-service",
                title=label,
                summary=summary,
                links=(
                    {
                        "title": "OpenAPI specification",
                        "url": parsed.spec_url,
                        "media_type": "application/vnd.oai.openapi+json",
                    },
                ),
                payload={
                    "group_key": group.key,
                    "spec_digest": parsed.digest,
                    "server_url": parsed.server_url,
                    "compiler_version": _COMPILER_VERSION,
                },
            )
            candidates.append(
                (
                    candidate,
                    f"{parsed.title} {parsed.description} {group.label} {summaries}",
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
        parsed = await self._load(source, public_http=_public_client(source))
        expected_digest = str(candidate.payload.get("spec_digest") or "")
        if expected_digest != parsed.digest:
            raise StaleCandidateError("OpenAPI specification changed; discover the source again")
        group_key = str(candidate.payload.get("group_key") or "")
        group = _find_group(parsed, group_key)
        document, warnings = compile_thing(source, parsed, group, candidate.external_id)
        return OnboardingResult(document=document, warnings=warnings)

    async def refresh_document(
        self,
        source: SourceDefinition,
        current_document: dict[str, Any],
        *,
        runtime: OnboardingRuntime,
        external_id: str = "",
    ) -> OnboardingResult:
        del runtime
        generation = current_document.get("wotbot:generation")
        if not isinstance(generation, dict) or generation.get("provider") != self.name:
            raise OpenApiError("Thing has no protected OpenAPI generation metadata")
        group_key = str(generation.get("groupKey") or "")
        generated_external_id = str(generation.get("externalId") or "")
        if not group_key or not generated_external_id:
            raise OpenApiError("Thing has incomplete OpenAPI generation metadata")
        if external_id and generated_external_id != external_id:
            raise OpenApiError("Thing origin does not match its OpenAPI generation metadata")
        parsed = await self._load(source, public_http=_public_client(source))
        group = _find_group(parsed, group_key)
        document, warnings = compile_thing(source, parsed, group, generated_external_id)
        return OnboardingResult(document=document, warnings=warnings)

    async def _load(
        self,
        source: SourceDefinition,
        *,
        public_http: BoundedHttpClient | None,
    ) -> ParsedApi:
        spec_url = str(source.get("url"))
        body, final_url = await fetch_spec(source, spec_url, public_http=public_http)
        if source.external_id and final_url != source.external_id:
            raise OpenApiError(
                "OpenAPI specification redirect target changed; register it as a new source"
            )
        document = parse_openapi(body)
        digest = hashlib.sha256(body).hexdigest()
        version = openapi_version(document)
        info = document.get("info") if isinstance(document.get("info"), dict) else {}
        title = _bounded_text(info.get("title"), 500) or "OpenAPI service"
        description = _bounded_text(info.get("description"), 2_000)
        server = resolve_server(
            document,
            final_url,
            str(source.get("server_url_override") or ""),
        )
        if source.network_access == "public":
            await resolve_public(server)
        security, security_definitions, security_warnings = select_security(document)
        operations, operation_warnings = collect_operations(document, security)
        if len(operations) > _MAX_OPERATIONS:
            raise OpenApiError("OpenAPI specification contains too many operations")
        if not operations:
            raise OpenApiError("OpenAPI specification has no supported operations")
        return ParsedApi(
            document=document,
            digest=digest,
            spec_url=final_url,
            version=version,
            title=title,
            description=description,
            server_url=server,
            security=security,
            security_definitions=security_definitions,
            operations=tuple(operations),
            warnings=_warnings((*security_warnings, *operation_warnings)),
        )


_MAX_DOCS_SCAN = 262_144
_DECLARED_SPEC_PATTERNS = (
    # The registered link relation for an API description.
    re.compile(r"""rel\s*=\s*["']service-desc["'][^>]*?href\s*=\s*["']([^"']{1,2048})["']""", re.I),
    re.compile(r"""href\s*=\s*["']([^"']{1,2048})["'][^>]*?rel\s*=\s*["']service-desc["']""", re.I),
    # ReDoc names it on the element; Swagger UI passes it in its config object.
    re.compile(r"""spec-?url\s*[=:]\s*["']([^"']{1,2048})["']""", re.I),
    re.compile(r"""(?:\burl|["']url["'])\s*:\s*["']([^"']{1,2048})["']""", re.I),
)


def _looks_like_specification(path: str) -> bool:
    return "openapi" in path or "swagger" in path or path.endswith((".json", ".yaml", ".yml"))


def declared_spec_url(payload: HttpPayload) -> str | None:
    """The API description an HTML documentation page names, if it names one.

    Swagger UI and ReDoc both configure themselves with their specification's
    URL, and ``service-desc`` is the registered link relation for it, so a docs
    page carries the pointer this provider needs. Only a same-origin candidate
    that looks like a document is returned, so following it cannot be steered
    into fetching an unrelated host.
    """

    if "html" not in payload.content_type.casefold():
        return None
    text = payload.text()[:_MAX_DOCS_SCAN]
    expected = origin_of(payload.url)
    for pattern in _DECLARED_SPEC_PATTERNS:
        for match in pattern.finditer(text):
            try:
                candidate = urljoin(payload.url, html.unescape(match.group(1).strip()))
                if not is_http_endpoint(candidate) or origin_of(candidate) != expected:
                    continue
                if not _looks_like_specification(urlparse(candidate).path.casefold()):
                    continue
            except ValueError:
                # A malformed link must not hide a later valid declaration.
                continue
            return candidate
    return None


def parse_openapi(body: bytes) -> dict[str, Any]:
    if len(body) > _MAX_SPEC_BYTES:
        raise OpenApiError("OpenAPI specification is too large")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            value = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise OpenApiError("OpenAPI specification is not valid JSON or YAML") from exc
    if not isinstance(value, dict):
        raise OpenApiError("OpenAPI specification must be an object")
    _check_complexity(value)
    openapi_version(value)
    if not isinstance(value.get("paths"), dict):
        raise OpenApiError("OpenAPI specification has no paths object")
    return value


def openapi_version(document: dict[str, Any]) -> str:
    openapi = str(document.get("openapi") or "")
    swagger = str(document.get("swagger") or "")
    if openapi.startswith(("3.0.", "3.1.")):
        return f"OpenAPI {openapi}"
    if swagger == "2.0":
        return "Swagger 2.0"
    raise OpenApiError("Document is not OpenAPI 3.0/3.1 or Swagger 2.0")


def resolve_server(document: dict[str, Any], spec_url: str, override: str) -> str:
    if override:
        server = override
    elif str(document.get("swagger") or "") == "2.0":
        parsed = urlparse(spec_url)
        schemes = document.get("schemes")
        scheme = (
            next(
                (str(value) for value in schemes if str(value) in {"http", "https"}),
                parsed.scheme,
            )
            if isinstance(schemes, list)
            else parsed.scheme
        )
        host = str(document.get("host") or parsed.netloc)
        base_path = str(document.get("basePath") or "")
        server = f"{scheme}://{host}{base_path}"
    else:
        servers = document.get("servers")
        entries = (
            [entry for entry in servers if isinstance(entry, dict)]
            if isinstance(servers, list)
            else []
        )
        if not entries:
            server = urljoin(spec_url, "/")
        else:
            server = ""
            for entry in entries:
                candidate = str(entry.get("url") or "")
                variables = entry.get("variables")
                unresolved = False
                for name in re.findall(r"\{([^{}]+)\}", candidate):
                    definition = variables.get(name) if isinstance(variables, dict) else None
                    default = definition.get("default") if isinstance(definition, dict) else None
                    if default is None:
                        unresolved = True
                        break
                    candidate = candidate.replace("{" + name + "}", str(default))
                candidate = urljoin(spec_url, candidate).rstrip("/")
                if not unresolved and is_http_endpoint(candidate):
                    server = candidate
                    break
            if not server:
                raise OpenApiError("OpenAPI specification has no valid server")
    server = server.rstrip("/")
    if not is_http_endpoint(server):
        raise OpenApiError("OpenAPI server is not an absolute HTTP(S) URL")
    return server


def select_security(
    document: dict[str, Any],
) -> tuple[SecurityChoice, dict[str, dict[str, Any]], tuple[str, ...]]:
    swagger = str(document.get("swagger") or "") == "2.0"
    raw_definitions = (
        document.get("securityDefinitions")
        if swagger
        else (document.get("components") or {}).get("securitySchemes")
        if isinstance(document.get("components"), dict)
        else None
    )
    definitions = raw_definitions if isinstance(raw_definitions, dict) else {}
    warnings: list[str] = []

    def choice_for(source_name: str) -> SecurityChoice | None:
        try:
            raw = _resolve(document, definitions.get(source_name), ())
        except OpenApiError:
            warnings.append(f"Security scheme '{source_name}' has an unsupported reference")
            return None
        if not isinstance(raw, dict):
            return None
        mapped = _map_security(source_name, raw, swagger=swagger)
        if mapped is None:
            warnings.append(f"Security scheme '{source_name}' is unsupported")
        return mapped

    def simple_requirement_name(requirement: Any) -> str | None:
        if not isinstance(requirement, dict) or len(requirement) != 1:
            warnings.append("Compound OpenAPI security requirements are unsupported")
            return None
        return str(next(iter(requirement)))

    top_level = document.get("security")
    top_level_requirements = top_level if isinstance(top_level, list) else []
    for requirement in top_level_requirements:
        source_name = simple_requirement_name(requirement)
        if source_name is None:
            continue
        mapped = choice_for(source_name)
        if mapped is not None:
            return _selected_security(mapped, warnings)

    # Some valid specifications, including Swagger Petstore, declare no
    # top-level security and put alternatives on each operation. Pick the
    # supported operation-level scheme covering the most operations so those
    # operations do not disappear merely because there is no global default.
    counts: dict[str, int] = {}
    choices: dict[str, SecurityChoice] = {}
    paths = document.get("paths")
    if isinstance(paths, dict):
        for raw_path in sorted(paths, key=str):
            try:
                path_item = _resolve(document, paths[raw_path], ())
            except OpenApiError:
                continue
            if not isinstance(path_item, dict):
                continue
            for method in _METHODS:
                try:
                    operation = _resolve(document, path_item.get(method), ())
                except OpenApiError:
                    continue
                if not isinstance(operation, dict) or "security" not in operation:
                    continue
                requirements = operation.get("security")
                if not isinstance(requirements, list):
                    continue
                seen: set[str] = set()
                for requirement in requirements:
                    source_name = simple_requirement_name(requirement)
                    if source_name is None or source_name in seen:
                        continue
                    seen.add(source_name)
                    mapped = choices.get(source_name)
                    if mapped is None:
                        mapped = choice_for(source_name)
                        if mapped is not None:
                            choices[source_name] = mapped
                    if mapped is not None:
                        counts[source_name] = counts.get(source_name, 0) + 1
    if counts:
        definition_order = {name: index for index, name in enumerate(definitions)}
        selected_name = min(
            counts,
            key=lambda name: (-counts[name], definition_order.get(name, len(definitions)), name),
        )
        return _selected_security(choices[selected_name], warnings)

    if not top_level_requirements:
        nosec = SecurityChoice("", "nosec_sc", {"scheme": "nosec"})
        return nosec, {nosec.td_name: nosec.definition}, _warnings(warnings)
    nosec = SecurityChoice(
        "__unsupported__",
        "nosec_sc",
        {"scheme": "nosec"},
        supported=False,
    )
    return nosec, {nosec.td_name: nosec.definition}, _warnings(warnings)


def _selected_security(
    selected: SecurityChoice, warnings: list[str]
) -> tuple[SecurityChoice, dict[str, dict[str, Any]], tuple[str, ...]]:
    return (
        selected,
        {
            selected.td_name: selected.definition,
            "nosec_sc": {"scheme": "nosec"},
        },
        _warnings(warnings),
    )


def _map_security(name: str, definition: dict[str, Any], *, swagger: bool) -> SecurityChoice | None:
    kind = str(definition.get("type") or "").lower()
    td_name = "openapi_" + _safe_name(name, "security")
    if kind == "basic" or (
        kind == "http" and str(definition.get("scheme") or "").lower() == "basic"
    ):
        return SecurityChoice(name, td_name, {"scheme": "basic"})
    if kind == "http" and str(definition.get("scheme") or "").lower() == "bearer":
        result = {"scheme": "bearer"}
        if value := _bounded_text(definition.get("bearerFormat"), 100):
            result["format"] = value
        return SecurityChoice(name, td_name, result)
    if kind == "apikey" and str(definition.get("in") or "").lower() == "header":
        header = _bounded_text(definition.get("name"), 200)
        if header:
            return SecurityChoice(
                name,
                td_name,
                {"scheme": "apikey", "in": "header", "name": header},
            )
    if swagger and kind == "oauth2":
        return None
    return None


def collect_operations(
    document: dict[str, Any],
    security: SecurityChoice,
    *,
    ignore_security: bool = False,
) -> tuple[list[Operation], tuple[str, ...]]:
    operations: list[Operation] = []
    warnings: list[str] = []
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return operations, ()
    operation_count = 0
    for raw_path in sorted(paths, key=str):
        if not isinstance(raw_path, str):
            warnings.append("Skipped a path with a non-string name")
            continue
        path = raw_path
        try:
            path_item = _resolve(document, paths[path], ())
        except OpenApiError as exc:
            warnings.append(f"Skipped path {path}: {exc}")
            continue
        if not isinstance(path_item, dict):
            warnings.append(f"Skipped path {path}: unresolved path item")
            continue
        common_parameters = path_item.get("parameters")
        for method in _METHODS:
            try:
                raw_operation = _resolve(document, path_item.get(method), ())
            except OpenApiError as exc:
                warnings.append(f"Skipped {method.upper()} {path}: {exc}")
                continue
            if not isinstance(raw_operation, dict):
                continue
            operation_count += 1
            if operation_count > _MAX_OPERATIONS:
                raise OpenApiError("OpenAPI specification contains too many operations")
            key = f"{method.upper()} {path}"
            try:
                operation, operation_warnings = _operation(
                    document,
                    path,
                    method,
                    raw_operation,
                    common_parameters,
                    security,
                    ignore_security=ignore_security,
                )
                warnings.extend(operation_warnings)
                if operation is not None:
                    operations.append(operation)
            except OpenApiError as exc:
                warnings.append(f"Skipped {key}: {exc}")
    return operations, _warnings(warnings)


def _operation(
    document: dict[str, Any],
    path: str,
    method: str,
    raw: dict[str, Any],
    common_parameters: Any,
    security: SecurityChoice,
    *,
    ignore_security: bool = False,
) -> tuple[Operation | None, list[str]]:
    key = f"{method.upper()} {path}"
    if not path.startswith("/") or "?" in path or "#" in path:
        raise OpenApiError("operation path is not a valid absolute path template")
    path_variables = set(re.findall(r"\{([^{}]+)\}", path))
    if "{" in re.sub(r"\{[^{}]+\}", "", path) or "}" in re.sub(r"\{[^{}]+\}", "", path):
        raise OpenApiError("operation path has an invalid URI template")
    if any(not _URI_VARIABLE_NAME.fullmatch(name) for name in path_variables):
        raise OpenApiError("operation path has an unsupported variable name")
    if raw.get("callbacks"):
        raise OpenApiError("callbacks are unsupported")
    operation_security = (
        "nosec_sc" if ignore_security else _operation_security(raw, document, security)
    )
    if operation_security is None:
        raise OpenApiError("operation uses an incompatible or unsupported security scheme")
    warnings: list[str] = []
    parameters: list[dict[str, Any]] = []
    input_schema: dict[str, Any] | None = None
    raw_parameters = [
        *(common_parameters if isinstance(common_parameters, list) else []),
        *(raw.get("parameters") if isinstance(raw.get("parameters"), list) else []),
    ]
    seen_parameters: set[tuple[str, str]] = set()
    for raw_parameter in raw_parameters:
        parameter = _resolve(document, raw_parameter, ())
        if not isinstance(parameter, dict):
            continue
        location = str(parameter.get("in") or "")
        name = str(parameter.get("name") or "")
        identity = (location, name)
        if not name or identity in seen_parameters:
            continue
        seen_parameters.add(identity)
        required = bool(parameter.get("required")) or location == "path"
        if location in {"header", "cookie", "formData"}:
            if required:
                raise OpenApiError(f"required {location} parameter '{name}' is unsupported")
            warnings.append(f"Ignored optional {location} parameter '{name}' on {key}")
            continue
        if location == "body":
            input_schema = _td_schema(document, parameter.get("schema"), ())
            continue
        if location not in {"path", "query"}:
            continue
        if not _URI_VARIABLE_NAME.fullmatch(name):
            if required:
                raise OpenApiError(f"required {location} parameter has an invalid name")
            warnings.append(f"Ignored optional {location} parameter with an invalid name on {key}")
            continue
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter
        resolved_schema = _td_schema(document, schema, ())
        if resolved_schema.get("type") not in {"string", "integer", "number", "boolean"}:
            if required:
                raise OpenApiError(f"required {location} parameter '{name}' is not primitive")
            warnings.append(
                f"Ignored optional non-primitive {location} parameter '{name}' on {key}"
            )
            continue
        parameters.append(
            {
                "name": name,
                "in": location,
                "required": required,
                "schema": resolved_schema,
                "description": _bounded_text(parameter.get("description"), 500),
            }
        )
    defined_path_variables = {
        str(parameter["name"]) for parameter in parameters if parameter["in"] == "path"
    }
    if defined_path_variables != path_variables:
        raise OpenApiError("path template variables do not match primitive path parameters")
    request_body = _resolve(document, raw.get("requestBody"), ())
    if isinstance(request_body, dict):
        content = request_body.get("content")
        _media_type, media = _json_content(content)
        if media is None:
            if request_body.get("required"):
                raise OpenApiError("required request body is not JSON")
            warnings.append(f"Ignored optional non-JSON request body on {key}")
        else:
            input_schema = _td_schema(document, media.get("schema"), ())
    output_schema, response_media_type = _response_schema(document, raw)
    operation_id = _bounded_text(raw.get("operationId"), 200)
    title = _bounded_text(raw.get("summary"), 500) or operation_id or key
    description = _bounded_text(raw.get("description"), 2_000)
    tags = raw.get("tags")
    tag = _bounded_text(tags[0], 200) if isinstance(tags, list) and tags else "untagged"
    return (
        Operation(
            key=key,
            method=method,
            path=path,
            operation_id=operation_id,
            title=title,
            description=description,
            tag=tag or "untagged",
            parameters=tuple(parameters),
            input_schema=input_schema,
            output_schema=output_schema,
            response_media_type=response_media_type,
            security_name=operation_security,
        ),
        warnings,
    )


def _operation_security(
    operation: dict[str, Any], document: dict[str, Any], selected: SecurityChoice
) -> str | None:
    if "security" not in operation:
        inherited = document.get("security")
        if inherited is None or inherited == []:
            return "nosec_sc"
        requirements = inherited if isinstance(inherited, list) else []
        if any(
            isinstance(requirement, dict)
            and len(requirement) == 1
            and str(next(iter(requirement))) == selected.source_name
            for requirement in requirements
        ):
            return selected.td_name if selected.supported else None
        return None
    requirements = operation.get("security")
    if requirements == []:
        return "nosec_sc"
    if not isinstance(requirements, list):
        return None
    for requirement in requirements:
        if (
            isinstance(requirement, dict)
            and len(requirement) == 1
            and str(next(iter(requirement))) == selected.source_name
        ):
            return selected.td_name
    return None


def _response_schema(
    document: dict[str, Any], operation: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None, None
    for status in sorted(responses, key=str):
        if not re.fullmatch(r"2\d\d", str(status)):
            continue
        response = _resolve(document, responses[status], ())
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        if isinstance(content, dict) and content:
            media_type, media = _json_content(content)
            if media is None:
                raise OpenApiError("successful response is not JSON")
            return _td_schema(document, media.get("schema"), ()), media_type
        schema = response.get("schema")
        if isinstance(schema, dict):
            produces = operation.get("produces") or document.get("produces")
            media_type = (
                str(produces[0]) if isinstance(produces, list) and produces else "application/json"
            )
            if not _is_json_media_type(media_type):
                raise OpenApiError("successful response is not JSON")
            return _td_schema(document, schema, ()), media_type
        return None, None
    return None, None


def operation_groups(operations: tuple[Operation, ...]) -> tuple[OperationGroup, ...]:
    ordered = tuple(sorted(operations, key=lambda item: (item.path, item.method, item.key)))
    if len(ordered) <= _MAX_GROUP_OPERATIONS:
        return (OperationGroup("all", "All operations", ordered),)
    grouped: dict[str, list[Operation]] = {}
    for operation in ordered:
        grouped.setdefault(operation.tag or "untagged", []).append(operation)
    result: list[OperationGroup] = []
    for tag in sorted(grouped, key=str.casefold):
        values = grouped[tag]
        for offset in range(0, len(values), _MAX_GROUP_OPERATIONS):
            part = offset // _MAX_GROUP_OPERATIONS + 1
            total_parts = (len(values) + _MAX_GROUP_OPERATIONS - 1) // _MAX_GROUP_OPERATIONS
            key = f"tag:{tag}:part:{part}"
            label = tag if total_parts == 1 else f"{tag} ({part}/{total_parts})"
            result.append(
                OperationGroup(
                    key,
                    label,
                    tuple(values[offset : offset + _MAX_GROUP_OPERATIONS]),
                )
            )
    return tuple(result)


def compile_thing(
    source: SourceDefinition,
    parsed: ParsedApi,
    group: OperationGroup,
    external_id: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    thing_id = provider_thing_id("openapi", source.id, external_id)
    actions: dict[str, Any] = {}
    used_names: set[str] = set()
    for operation in group.operations:
        action_name = _unique_action_name(operation, used_names)
        actions[action_name] = _compile_action(parsed, operation)
    title = parsed.title if group.key == "all" else f"{parsed.title} — {group.label}"
    generated_security = {
        name: {**definition, "wotbot:generatedBy": "openapi"}
        for name, definition in parsed.security_definitions.items()
    }
    document: dict[str, Any] = {
        "@context": [TD_CONTEXT, WOTBOT_CONTEXT],
        "id": thing_id,
        "title": title[:500],
        "description": parsed.description,
        "security": [parsed.security.td_name],
        "securityDefinitions": generated_security,
        "links": [
            {
                "href": parsed.spec_url,
                "rel": "describedby",
                "type": "application/vnd.oai.openapi+json",
                "wotbot:generatedBy": "openapi",
            }
        ],
        "wotbot:generation": {
            "provider": "openapi",
            "compilerVersion": _COMPILER_VERSION,
            "specificationDigest": parsed.digest,
            "groupKey": group.key,
            "externalId": external_id,
            "operationKeys": [operation.key for operation in group.operations],
            "serverUrl": parsed.server_url,
        },
        "actions": actions,
    }
    if (
        len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())
        > _MAX_TD_BYTES
    ):
        raise OpenApiError("Generated Thing Description is too large")
    return document, parsed.warnings


def _compile_action(parsed: ParsedApi, operation: Operation) -> dict[str, Any]:
    query_parameters = [item for item in operation.parameters if item["in"] == "query"]
    href = parsed.server_url + (
        operation.path if operation.path.startswith("/") else "/" + operation.path
    )
    if query_parameters:
        href += "{?" + ",".join(item["name"] for item in query_parameters) + "}"
    form: dict[str, Any] = {
        "href": href,
        "op": ["invokeaction"],
        "htv:methodName": operation.method.upper(),
        "contentType": "application/json",
        "security": [operation.security_name],
    }
    if operation.response_media_type:
        form["response"] = {"contentType": operation.response_media_type}
    return _action_from_form(operation, form, generated_by="openapi")


def _action_from_form(
    operation: Operation,
    form: dict[str, Any],
    *,
    generated_by: str,
) -> dict[str, Any]:
    path_parameters = [item for item in operation.parameters if item["in"] == "path"]
    query_parameters = [item for item in operation.parameters if item["in"] == "query"]
    action: dict[str, Any] = {
        "title": operation.title,
        "description": operation.description,
        "safe": operation.method in {"get", "head"},
        "idempotent": operation.method in {"get", "head", "put", "delete", "options"},
        "wotbot:generatedBy": generated_by,
        "wotbot:operationKey": operation.key,
        "forms": [form],
    }
    if operation.parameters:
        action["uriVariables"] = {
            item["name"]: {
                **item["schema"],
                **({"description": item["description"]} if item.get("description") else {}),
            }
            for item in (*path_parameters, *query_parameters)
        }
    if operation.input_schema:
        action["input"] = operation.input_schema
    if operation.output_schema:
        action["output"] = operation.output_schema
    return action


def compile_provider_actions(
    document: dict[str, Any],
    *,
    thing_id: str,
    provider: str,
    max_operations: int = _MAX_GROUP_OPERATIONS,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], int]:
    """Compile endpoint-neutral OpenAPI operations into local provider actions.

    The provider binding carries no external endpoint. URI variables are
    expanded into its local query solely so the binding can forward their
    values to the trusted dispatcher; the dispatcher resolves the real target
    from protected generation metadata.
    """

    no_security = SecurityChoice("", "nosec_sc", {"scheme": "nosec"})
    operations, operation_warnings = collect_operations(
        document,
        no_security,
        ignore_security=True,
    )
    ordered = sorted(operations, key=lambda item: (item.path, item.method, item.key))
    selected = ordered[:max_operations]
    warnings = list(operation_warnings)
    if len(ordered) > len(selected):
        warnings.append(
            f"Generated the first {len(selected)} of {len(ordered)} supported operations"
        )

    actions: dict[str, Any] = {}
    used_names: set[str] = set()
    for operation in selected:
        action_name = _unique_action_name(operation, used_names)
        path_variables = [
            str(item["name"]) for item in operation.parameters if item["in"] == "path"
        ]
        query_variables = [
            str(item["name"]) for item in operation.parameters if item["in"] == "query"
        ]
        variables = [*path_variables, *query_variables]
        href = provider_action_href(thing_id, action_name)
        if variables:
            href += "{?" + ",".join(variables) + "}"
        form: dict[str, Any] = {
            "href": href,
            "op": ["invokeaction"],
            "htv:methodName": operation.method.upper(),
            "contentType": "application/json",
            "security": ["nosec_sc"],
            "wotbot:providerOperation": "invoke",
            "wotbot:httpMethod": operation.method.upper(),
            "wotbot:path": operation.path,
            "wotbot:pathVariables": path_variables,
            "wotbot:queryVariables": query_variables,
        }
        if operation.response_media_type:
            form["response"] = {"contentType": operation.response_media_type}
        actions[action_name] = _action_from_form(
            operation,
            form,
            generated_by=provider,
        )
    return (
        actions,
        tuple(operation.key for operation in selected),
        _warnings(warnings),
        len(ordered),
    )


def _td_schema(document: dict[str, Any], raw: Any, stack: tuple[str, ...]) -> dict[str, Any]:
    resolved = _resolve(document, raw, stack)
    if not isinstance(resolved, dict):
        return {}
    counter = [0]

    def convert(value: Any, depth: int) -> dict[str, Any]:
        counter[0] += 1
        if depth > _MAX_SCHEMA_DEPTH or counter[0] > _MAX_SCHEMA_NODES:
            raise OpenApiError("schema is too complex")
        item = _resolve(document, value, stack)
        if not isinstance(item, dict):
            return {}
        result: dict[str, Any] = {}
        for key in (
            "type",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "readOnly",
            "writeOnly",
        ):
            if key in item:
                result[key] = item[key]
        for key, limit in (("title", 200), ("description", 1_000), ("format", 100)):
            if key in item:
                result[key] = _bounded_text(item[key], limit)
        enum = item.get("enum")
        if isinstance(enum, list):
            result["enum"] = [
                _bounded_schema_value(value) for value in enum[:100] if _is_schema_scalar(value)
            ]
        for key in ("const", "default"):
            # Only when the schema actually carries one: `item.get` cannot tell
            # an absent key from an explicit null, and writing `const: null`
            # into every node asserts that the value must be null.
            if key in item and _is_schema_scalar(item[key]):
                result[key] = _bounded_schema_value(item[key])
        schema_type = result.get("type")
        if isinstance(schema_type, list):
            non_null_types = list(
                dict.fromkeys(
                    value for value in schema_type if isinstance(value, str) and value != "null"
                )
            )
            if len(non_null_types) == 1:
                result["type"] = non_null_types[0]
            else:
                result.pop("type", None)
        elif schema_type not in {
            None,
            "null",
            "boolean",
            "integer",
            "number",
            "string",
            "object",
            "array",
        }:
            result.pop("type", None)
        if isinstance(item.get("properties"), dict):
            result["type"] = result.get("type") or "object"
            result["properties"] = {
                str(name): convert(schema, depth + 1)
                for name, schema in list(item["properties"].items())[:200]
            }
            required = item.get("required")
            if isinstance(required, list):
                result["required"] = [str(name) for name in required[:200]]
        if "items" in item:
            result["type"] = result.get("type") or "array"
            result["items"] = convert(item.get("items"), depth + 1)
        for union_key in ("oneOf", "anyOf"):
            branches = item.get(union_key)
            if not isinstance(branches, list):
                continue
            variants = [
                child for child in branches[:20] if not _is_null_schema(document, child, stack)
            ]
            if len(variants) == 1:
                # OpenAPI 3.1 spells an optional T as `anyOf: [T, "null"]`, which
                # is what FastAPI and Pydantic emit for every optional parameter.
                # Collapsing it back to T keeps the declared type, so the
                # parameter survives instead of being dropped as non-primitive.
                for key, value in convert(variants[0], depth + 1).items():
                    result.setdefault(key, value)
            elif variants:
                result[union_key] = [convert(child, depth + 1) for child in variants]
        if isinstance(item.get("allOf"), list):
            for child in item["allOf"][:20]:
                converted = convert(child, depth + 1)
                if converted.get("properties"):
                    result.setdefault("type", "object")
                    result.setdefault("properties", {}).update(converted["properties"])
                if converted.get("required"):
                    result["required"] = list(
                        dict.fromkeys([*result.get("required", []), *converted["required"]])
                    )
        return result

    return convert(resolved, 0)


def _is_null_schema(document: dict[str, Any], value: Any, stack: tuple[str, ...]) -> bool:
    """Whether a union branch is the null that makes the union nullable."""

    resolved = _resolve(document, value, stack)
    return isinstance(resolved, dict) and resolved.get("type") == "null"


def _resolve(document: dict[str, Any], value: Any, stack: tuple[str, ...]) -> Any:
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = str(value.get("$ref") or "")
    if not ref.startswith("#/"):
        raise OpenApiError("external references are unsupported")
    if ref in stack or len(stack) >= _MAX_SCHEMA_DEPTH:
        raise OpenApiError("cyclic or excessively deep reference")
    current: Any = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise OpenApiError(f"unresolved reference '{ref}'")
        current = current[part]
    return _resolve(document, current, (*stack, ref))


async def fetch_spec(
    source: SourceDefinition,
    url: str,
    *,
    public_http: BoundedHttpClient | None,
) -> tuple[bytes, str]:
    headers = {
        "Accept": (
            "application/vnd.oai.openapi+json,application/json,application/yaml,"
            "text/yaml,application/x-yaml"
        ),
        **credential_headers(source),
    }
    client = public_http or source_client(source, max_bytes=_MAX_SPEC_BYTES)
    response = await client.get(
        url,
        headers=headers,
        max_bytes=_MAX_SPEC_BYTES,
        credentialed=bool(credential_headers(source)),
    )
    if response.status in {401, 403}:
        raise SourceAuthenticationError("OpenAPI source rejected its credential")
    if response.status < 200 or response.status >= 300:
        raise OpenApiError(f"OpenAPI source returned HTTP {response.status}")
    return response.body, response.url
    raise OpenApiError("OpenAPI source redirect limit exceeded")


def group_external_id(source_identity: str, server_url: str, group_key: str) -> str:
    digest = hashlib.sha256(f"{source_identity}\0{server_url}\0{group_key}".encode()).hexdigest()
    return f"api:{digest}"


def _find_group(parsed: ParsedApi, group_key: str) -> OperationGroup:
    group = next(
        (item for item in operation_groups(parsed.operations) if item.key == group_key), None
    )
    if group is None:
        raise OpenApiError("OpenAPI operation group is no longer available")
    return group


def _unique_action_name(operation: Operation, used: set[str]) -> str:
    base = _safe_name(operation.operation_id, "")
    if not base:
        base = _safe_name(f"{operation.method}_{operation.path}", "operation")
    name = base[:100]
    if name in used:
        suffix = hashlib.sha256(operation.key.encode()).hexdigest()[:8]
        name = f"{name[:91]}_{suffix}"
    used.add(name)
    return name


def _safe_name(value: str, fallback: str) -> str:
    name = _NAME.sub("_", str(value or "")).strip("_")
    if name and name[0].isdigit():
        name = "op_" + name
    return name or fallback


def _json_content(value: Any) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(value, dict):
        return None, None
    for media_type, media in value.items():
        if _is_json_media_type(str(media_type)) and isinstance(media, dict):
            return str(media_type), media
    return None, None


def _is_json_media_type(value: str) -> bool:
    normalized = value.split(";", 1)[0].strip().lower()
    return normalized in _JSON_MEDIA_TYPES or normalized.endswith("+json")


def _check_complexity(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_DOCUMENT_NODES or depth > _MAX_DOCUMENT_DEPTH:
            raise OpenApiError("OpenAPI specification is too complex")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _warnings(values: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value)[:300] for value in values if str(value).strip()))[:20]


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _is_schema_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _bounded_schema_value(value: Any) -> Any:
    return value[:1_000] if isinstance(value, str) else value


def _public_client(source: SourceDefinition) -> BoundedHttpClient | None:
    return (
        BoundedHttpClient(max_requests=8, max_bytes=_MAX_SPEC_BYTES)
        if source.network_access == "public"
        else None
    )


__all__ = [
    "OpenApiError",
    "OpenApiProvider",
    "collect_operations",
    "compile_provider_actions",
    "compile_thing",
    "declared_spec_url",
    "openapi_version",
    "operation_groups",
    "parse_openapi",
    "resolve_server",
    "select_security",
]
