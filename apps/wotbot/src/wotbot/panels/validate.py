"""Pre-flight checks for agent-authored panel markup.

A generated panel is written blind. Nothing executes it between the model
emitting it and the browser loading it, so a stray bracket or an affordance the
Thing does not actually have shows up as a dead card and a console message the
agent never sees -- the panel looks delivered and simply does nothing. These
checks run at creation time and turn both classes of mistake into a tool error
the agent can fix in the same turn.

Everything here works off real parse trees (tree-sitter's HTML and JavaScript
grammars) rather than pattern matching. The rules that decide where a script
ends, whether a `/` opens a regular expression or divides, and what is a call
are subtle enough that hand-rolled scanning gets them wrong in the expensive
direction: rejecting a panel that would have worked. Parsing also means the
checks see code as code -- a `fetch(` inside a comment or a string is not a
network call, and neither is `thing.fetch()`.

Still deliberately conservative about what it reports. A false rejection costs a
retry loop and a confused model, so only unambiguous mistakes count: syntax the
grammar cannot parse, `wot` calls whose thing and affordance are both plain
string literals, unavailable static dependencies, and egress APIs the panel CSP
blocks outright.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import tree_sitter_html
import tree_sitter_javascript
from tree_sitter import Language, Node, Parser, Tree

# Anything else (importmap, application/json, text/template) is data the browser
# never runs.
_EXECUTABLE_SCRIPT_TYPES = frozenset({"", "module", "text/javascript", "application/javascript"})

_BRIDGE_OPS = frozenset(
    {
        "readProperty",
        "writeProperty",
        "invokeAction",
        "observeProperty",
        "subscribeEvent",
    }
)

_OP_AFFORDANCE_KIND = {
    "readProperty": "properties",
    "writeProperty": "properties",
    "observeProperty": "properties",
    "invokeAction": "actions",
    "subscribeEvent": "events",
}

_AFFORDANCE_KINDS = ("properties", "actions", "events")

# CSP has no 'self' and no wildcard in connect-src, so none of these fails
# loudly -- the request is blocked and the panel sits on a promise that never
# settles.
_BLOCKED_FUNCTIONS = {"fetch": "fetch()"}
_BLOCKED_CONSTRUCTORS = {
    "XMLHttpRequest": "XMLHttpRequest",
    "WebSocket": "WebSocket",
    "EventSource": "EventSource",
}
_BLOCKED_METHODS = {"sendBeacon": "navigator.sendBeacon()"}

# Mirrors SCRIPT_CDNS and CDN_HOSTS in apps/ui/src/lib/panel-csp.ts, which is
# the policy the browser enforces; test_panel_validation keeps the two in step.
_SCRIPT_HOSTS = frozenset(
    {
        "cdn.jsdelivr.net",
        "unpkg.com",
        "cdnjs.cloudflare.com",
        "cdn.plot.ly",
    }
)
_DEPENDENCY_HOSTS = _SCRIPT_HOSTS | {"fonts.googleapis.com", "fonts.gstatic.com"}
# Crawl bounds. Hitting one says nothing about the panel, so the crawl stops
# quietly instead of reporting a problem.
_MAX_DEPENDENCIES = 24
_MAX_REDIRECTS = 3
_DEPENDENCY_TIMEOUT_SECONDS = 5.0
_DEPENDENCY_DEADLINE_SECONDS = 8.0
_MAX_MODULE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class PanelScript:
    """One executable ``<script>`` body and where it starts in the markup."""

    source: str
    first_line: int


@dataclass(frozen=True)
class PanelDependency:
    """One browser-loaded script, module, stylesheet, or import-map target."""

    url: str
    kind: str


@dataclass(frozen=True)
class ModuleMap:
    """Browser import-map entries used to resolve bare module specifiers."""

    imports: dict[str, str]
    scopes: dict[str, dict[str, str]]


@dataclass(frozen=True)
class DependencyProbe:
    """Result of checking one external dependency."""

    problem: str | None
    final_url: str
    module_source: str | None = None


# Loading a grammar is the costly part and the result is immutable; a Parser
# holds mutable state, so callers get their own rather than sharing one.
@lru_cache(maxsize=1)
def _html_language() -> Language:
    return Language(tree_sitter_html.language())


@lru_cache(maxsize=1)
def _js_language() -> Language:
    return Language(tree_sitter_javascript.language())


def _parse_html(html: str) -> Tree:
    return Parser(_html_language()).parse(html.encode("utf-8"))


def _parse_js(source: str) -> Tree:
    return Parser(_js_language()).parse(source.encode("utf-8"))


def _walk(node: Node) -> Iterator[Node]:
    """Every node under `node`, including itself, in document order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _text(node: Node | None) -> str:
    return "" if node is None or node.text is None else node.text.decode("utf-8", "replace")


def _attribute(start_tag: Node, name: str) -> str:
    for attribute in start_tag.children:
        if attribute.type != "attribute":
            continue
        key = next((c for c in attribute.children if c.type == "attribute_name"), None)
        if _text(key).lower() != name:
            continue
        for child in attribute.children:
            if child.type == "quoted_attribute_value":
                inner = next((c for c in child.children if c.type == "attribute_value"), None)
                return _text(inner)
            if child.type == "attribute_value":
                return _text(child)
    return ""


def _tag_name(start_tag: Node) -> str:
    tag = next((child for child in start_tag.children if child.type == "tag_name"), None)
    return _text(tag).lower()


def extract_scripts(html: str) -> list[PanelScript]:
    """The executable script bodies in a panel's markup, in document order.

    The HTML grammar ends a script at the first ``</script>`` exactly as the
    browser's tokenizer does, so a panel that closes one early here is broken
    there too.
    """
    tree = _parse_html(html)
    scripts: list[PanelScript] = []
    for node in _walk(tree.root_node):
        if node.type != "script_element":
            continue
        start_tag = next((c for c in node.children if c.type == "start_tag"), None)
        script_type = _attribute(start_tag, "type").strip().lower() if start_tag else ""
        if script_type not in _EXECUTABLE_SCRIPT_TYPES:
            continue
        body = next((c for c in node.children if c.type == "raw_text"), None)
        if body is not None:
            scripts.append(PanelScript(_text(body), body.start_point[0] + 1))
    return scripts


def extract_dependencies(html: str) -> list[PanelDependency]:
    """External resources whose availability can be checked before rendering.

    Bare module specifiers are left out: what they name depends on the import
    map, which validate_external_dependencies applies.
    """
    return _scan_document(html).dependencies


@dataclass(frozen=True)
class _DocumentScan:
    dependencies: list[PanelDependency]
    inline_specifiers: list[str]
    module_map: ModuleMap
    problems: list[str]


def _scan_document(html: str) -> _DocumentScan:
    """Collect dependencies, inline module imports, and the import map in one walk."""
    dependencies: list[PanelDependency] = []
    inline_specifiers: list[str] = []
    imports: dict[str, str] = {}
    scopes: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for node in _walk(_parse_html(html).root_node):
        if node.type not in ("script_element", "element"):
            continue
        start_tag = next((child for child in node.children if child.type == "start_tag"), None)
        if start_tag is None:
            continue
        tag_name = _tag_name(start_tag)
        if tag_name == "script":
            script_type = _attribute(start_tag, "type").strip().lower()
            body = next((child for child in node.children if child.type == "raw_text"), None)
            if script_type == "importmap":
                problems.extend(_merge_import_map(_text(body), imports, scopes))
                continue
            src = _attribute(start_tag, "src").strip()
            if src:
                kind = "module" if script_type == "module" else "script"
                dependencies.append(PanelDependency(src, kind))
            if body is not None and script_type == "module":
                for specifier in _module_specifiers(_text(body)):
                    inline_specifiers.append(specifier)
                    if not _is_bare_specifier(specifier):
                        dependencies.append(PanelDependency(specifier, "module"))
        elif tag_name == "link":
            rel = {part.lower() for part in _attribute(start_tag, "rel").split()}
            href = _attribute(start_tag, "href").strip()
            if not href:
                continue
            # rel=preload is left out: its `as` decides which CSP directive
            # applies, and an image preload may legitimately name a tile host.
            if "modulepreload" in rel:
                dependencies.append(PanelDependency(href, "module"))
            elif "stylesheet" in rel:
                dependencies.append(PanelDependency(href, "stylesheet"))

    # Keep browser order, while avoiding repeated probes for a shared resource.
    return _DocumentScan(
        list(dict.fromkeys(dependencies)),
        inline_specifiers,
        ModuleMap(imports, scopes),
        problems,
    )


def _merge_import_map(
    source: str,
    imports: dict[str, str],
    scopes: dict[str, dict[str, str]],
) -> list[str]:
    try:
        payload = json.loads(source)
    except ValueError:
        return ["The panel contains an import map that is not valid JSON."]
    if not isinstance(payload, dict):
        return ["The panel import map must be a JSON object."]
    raw_imports = payload.get("imports")
    if isinstance(raw_imports, dict):
        imports.update(_string_mappings(raw_imports))
    raw_scopes = payload.get("scopes")
    if isinstance(raw_scopes, dict):
        for scope, mappings in raw_scopes.items():
            if isinstance(scope, str) and isinstance(mappings, dict):
                scopes.setdefault(scope, {}).update(_string_mappings(mappings))
    return []


def _string_mappings(raw: Mapping[Any, Any]) -> dict[str, str]:
    return {
        key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
    }


def _is_bare_specifier(specifier: str) -> bool:
    return not (urlsplit(specifier).scheme or specifier.startswith(("//", "/", "./", "../")))


def _module_specifiers(source: str) -> list[str]:
    """Static `import ... from` and `export ... from` sources, in source order.

    Dynamic `import()` is skipped: it may sit behind a branch that never runs.
    """
    specifiers = []
    for node in _walk(_parse_js(source).root_node):
        if node.type not in ("import_statement", "export_statement"):
            continue
        value = node.child_by_field_name("source")
        if value is not None and (specifier := _string_literal(value)):
            specifiers.append(specifier)
    return specifiers


def find_syntax_error(source: str, line_offset: int = 0) -> str | None:
    """Describe the first thing the grammar cannot parse, or None if it parses."""
    return _describe_error(_parse_js(source), line_offset)


def _describe_error(tree: Tree, line_offset: int) -> str | None:
    if not tree.root_node.has_error:
        return None
    node = _first_error(tree.root_node)
    if node is None:
        return f"the script starting on line {line_offset + 1} does not parse"
    line = node.start_point[0] + 1 + line_offset
    if node.is_missing:
        # tree-sitter names the token it expected, which is the same thing the
        # browser says: "missing ) after argument list".
        return f"missing '{node.type}' on line {line}"
    snippet = _text(node).strip().splitlines()
    if snippet and snippet[0]:
        return f"unexpected input on line {line}: {snippet[0][:60]!r}"
    return f"unexpected input on line {line}"


def _first_error(node: Node) -> Node | None:
    """The earliest ERROR or MISSING node, descending only where an error is."""
    if node.is_missing or node.type == "ERROR":
        return node
    for child in node.children:
        if child.has_error or child.is_missing:
            found = _first_error(child)
            if found is not None:
                return found
    return None


def validate_panel(
    html: str,
    capabilities: Iterable[Mapping[str, Any]],
    thing_affordances: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
) -> list[str]:
    """Problems that would make this panel silently fail, in reading order.

    `thing_affordances` maps a thing id to its Thing Description affordances
    (``{"properties": [...], "actions": [...], "events": [...]}``). Only ids
    present are checked, so a registry lookup that failed for its own reasons
    never turns into a complaint about the panel.
    """
    declared = [capability for capability in capabilities if capability.get("thingId")]
    parsed = [(script, _parse_js(script.source)) for script in extract_scripts(html)]

    problems: list[str] = []
    problems.extend(_integrity_problems(html))
    problems.extend(_syntax_problems(parsed))
    problems.extend(_capability_problems(declared, thing_affordances or {}))
    problems.extend(_bridge_call_problems(parsed, declared))
    problems.extend(_egress_problems(parsed))
    return problems


def _integrity_problems(html: str) -> list[str]:
    """Reject unverifiable SRI hashes authored from model memory.

    A wrong ``integrity`` value makes the browser discard an otherwise valid
    CDN resource.  Panel generation has no network access with which to verify
    the bytes behind a URL, so accepting a model-supplied hash turns a typo into
    a broken interface.  The panel CSP already restricts which CDN hosts may
    serve dependencies; generated panels should rely on that allowlist instead.
    """
    tree = _parse_html(html)
    for node in _walk(tree.root_node):
        if node.type != "attribute":
            continue
        name = next((child for child in node.children if child.type == "attribute_name"), None)
        if _text(name).lower() == "integrity":
            return [
                (
                    "The panel includes an integrity attribute that cannot be verified "
                    "during generation. A wrong hash makes the browser block the CDN "
                    "resource. Remove every integrity attribute and rely on the panel "
                    "CSP's CDN allowlist instead."
                )
            ]
    return []


async def validate_external_dependencies(
    html: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Check all statically discoverable panel dependencies without executing code.

    Only the same fixed hosts allowed by the panel CSP are contacted, which
    prevents generated markup from turning this check into an SSRF primitive.
    Transient network failures and server errors stay silent; a definite client
    error or a redirect outside the allowlist is actionable panel breakage. The
    whole check is bounded in time and size, and running out of either only
    ends it early: problems already found are still reported.
    """
    scan = _scan_document(html)
    problems = list(scan.problems)
    dependencies = list(scan.dependencies)
    for specifier in scan.inline_specifiers:
        if not _is_bare_specifier(specifier):
            continue
        resolved = _resolve_module_specifier(specifier, None, scan.module_map)
        if resolved is None:
            problems.append(_unresolved_module_problem(specifier, None))
        else:
            dependencies.append(PanelDependency(resolved, "module"))

    try:
        async with asyncio.timeout(_DEPENDENCY_DEADLINE_SECONDS):
            await _crawl_dependencies(dependencies, scan.module_map, problems, transport)
    except TimeoutError:
        pass
    return list(dict.fromkeys(problems))


async def _crawl_dependencies(
    dependencies: list[PanelDependency],
    module_map: ModuleMap,
    problems: list[str],
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Probe dependencies breadth-first, appending to `problems` as they are found."""
    seen: set[PanelDependency] = set()
    pending = list(dict.fromkeys(dependencies))
    async with httpx.AsyncClient(
        transport=transport,
        timeout=_DEPENDENCY_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        while pending:
            wave = pending[: _MAX_DEPENDENCIES - len(seen)]
            seen.update(wave)
            # Each probe records its own findings as it finishes, so a deadline
            # hit while a slow sibling is outstanding keeps what came back.
            discovered = await asyncio.gather(
                *(
                    _probe_and_record(client, dependency, module_map, problems)
                    for dependency in wave
                )
            )
            pending = [
                candidate
                for candidate in dict.fromkeys(item for items in discovered for item in items)
                if candidate not in seen
            ]


async def _probe_and_record(
    client: httpx.AsyncClient,
    dependency: PanelDependency,
    module_map: ModuleMap,
    problems: list[str],
) -> list[PanelDependency]:
    """Probe one dependency, record its problems, and return the modules it imports."""
    result = await _probe_dependency(client, dependency)
    if result.problem is not None:
        problems.append(result.problem)
        return []
    if dependency.kind != "module" or result.module_source is None:
        return []
    imported = []
    for specifier in _module_specifiers(result.module_source):
        resolved = _resolve_module_specifier(specifier, result.final_url, module_map)
        if resolved is None:
            problems.append(_unresolved_module_problem(specifier, result.final_url))
        else:
            imported.append(PanelDependency(resolved, "module"))
    return imported


def _resolve_module_specifier(
    specifier: str,
    importer_url: str | None,
    module_map: ModuleMap,
) -> str | None:
    parsed = urlsplit(specifier)
    if parsed.scheme:
        return specifier
    if specifier.startswith("//"):
        return f"https:{specifier}"
    if specifier.startswith(("/", "./", "../")):
        return urljoin(importer_url, specifier) if importer_url else specifier

    mappings = module_map.imports
    if importer_url:
        matching_scopes = [scope for scope in module_map.scopes if importer_url.startswith(scope)]
        if matching_scopes:
            mappings = {
                **module_map.imports,
                **module_map.scopes[max(matching_scopes, key=len)],
            }
    return _resolve_import_map_entry(specifier, mappings)


def _resolve_import_map_entry(specifier: str, mappings: Mapping[str, str]) -> str | None:
    if specifier in mappings:
        return mappings[specifier]
    prefixes = [key for key in mappings if key.endswith("/") and specifier.startswith(key)]
    if not prefixes:
        return None
    prefix = max(prefixes, key=len)
    target = mappings[prefix]
    if not target.endswith("/"):
        return None
    return f"{target}{specifier[len(prefix) :]}"


def _unresolved_module_problem(specifier: str, importer_url: str | None) -> str:
    location = f" imported by {importer_url!r}" if importer_url else " in an inline module"
    return (
        f"The bare module specifier {specifier!r}{location} has no matching import-map "
        "entry, so the browser cannot resolve it. Add an import-map mapping or use a "
        "browser-resolvable module URL."
    )


async def _probe_dependency(
    client: httpx.AsyncClient,
    dependency: PanelDependency,
) -> DependencyProbe:
    current = dependency.url
    if current.startswith("//"):
        current = f"https:{current}"

    for _redirect in range(_MAX_REDIRECTS + 1):
        parsed = urlsplit(current)
        hosts = _DEPENDENCY_HOSTS if dependency.kind == "stylesheet" else _SCRIPT_HOSTS
        if parsed.scheme != "https" or parsed.hostname not in hosts:
            return DependencyProbe(
                (
                    f"The external {dependency.kind} {dependency.url!r} cannot load under the "
                    "panel CSP. Use an HTTPS URL on one of the permitted CDN hosts."
                ),
                current,
            )
        try:
            # One byte past the limit, so a truncated 206 is recognised as too
            # large rather than parsed as if it were the whole module.
            range_end = _MAX_MODULE_BYTES if dependency.kind == "module" else 0
            async with client.stream(
                "GET",
                current,
                headers={"Range": f"bytes=0-{range_end}"},
            ) as response:
                status = response.status_code
                location = response.headers.get("location")
                module_source = None
                if 200 <= status < 300 and dependency.kind == "module":
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_MODULE_BYTES:
                            body = bytearray()
                            break
                    if body:
                        module_source = body.decode("utf-8", "replace")
        except httpx.HTTPError:
            # Do not reject a good panel because validation temporarily lacks
            # DNS, internet access, or a responsive CDN.
            return DependencyProbe(None, current)

        if 200 <= status < 300:
            return DependencyProbe(None, current, module_source)
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        if 400 <= status < 500 and status not in {408, 429}:
            return DependencyProbe(
                (
                    f"The external {dependency.kind} {dependency.url!r} is unavailable "
                    f"(HTTP {status}). Correct or replace the URL and retry."
                ),
                current,
            )
        return DependencyProbe(None, current)

    # Browsers follow far longer chains; running out of hops here proves nothing.
    return DependencyProbe(None, current)


def _syntax_problems(parsed: list[tuple[PanelScript, Tree]]) -> list[str]:
    problems = []
    for script, tree in parsed:
        error = _describe_error(tree, script.first_line - 1)
        if error is None:
            continue
        problems.append(
            f"The panel does not parse: {error}. Line numbers are those of the "
            "html you passed. Re-emit the complete panel with that fixed."
        )
    return problems


def _capability_problems(
    declared: list[Mapping[str, Any]],
    thing_affordances: Mapping[str, Mapping[str, Iterable[str]]],
) -> list[str]:
    problems = []
    for capability in declared:
        thing_id = capability["thingId"]
        known = thing_affordances.get(thing_id)
        if known is None:
            continue
        ops = list(capability.get("ops") or [])
        available = {kind: sorted(set(known.get(kind) or ())) for kind in _AFFORDANCE_KINDS}
        expected = sorted({_OP_AFFORDANCE_KIND[op] for op in ops if op in _OP_AFFORDANCE_KIND})
        for name in capability.get("affordances") or []:
            if any(name in available[kind] for kind in expected):
                continue
            elsewhere = [kind for kind in _AFFORDANCE_KINDS if name in available[kind]]
            if elsewhere:
                label = {
                    "properties": "a property",
                    "actions": "an action",
                    "events": "an event",
                }[elsewhere[0]]
                problems.append(
                    f"'{name}' on {thing_id} is {label}, but the declared ops are "
                    f"{', '.join(ops)}. Declare the op that matches its kind, or use a "
                    "different affordance."
                )
            else:
                problems.append(
                    f"{thing_id} has no '{name}'. Its affordances are: "
                    + "; ".join(f"{kind} {names}" for kind, names in available.items() if names)
                    + ". Inspect the Thing and declare a name it actually has."
                )
    return problems


def _bridge_call_problems(
    parsed: list[tuple[PanelScript, Tree]],
    declared: list[Mapping[str, Any]],
) -> list[str]:
    problems = []
    seen: set[tuple[str, str, str]] = set()
    for _script, tree in parsed:
        for op, thing_id, name in _bridge_calls(tree):
            if (op, thing_id, name) in seen:
                continue
            seen.add((op, thing_id, name))
            if _is_allowed(declared, op, thing_id, name):
                continue
            problems.append(
                f"The panel calls wot.{op}('{thing_id}', '{name}') but no declared "
                "capability permits it, so the bridge will reject the call at runtime. "
                "Add it to `capabilities` or drop the call."
            )
    return problems


def _bridge_calls(tree: Tree) -> Iterator[tuple[str, str, str]]:
    """Every ``wot.<op>('thing', 'name')`` whose first two arguments are literals.

    Calls that compute either argument are skipped rather than guessed at: the
    panel may well be right, and a wrong complaint is worse than a missed one.
    """
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        op = _text(function.child_by_field_name("property"))
        if op not in _BRIDGE_OPS or not _is_wot_object(function.child_by_field_name("object")):
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            continue
        # Comments are named nodes too, and an argument list is allowed to be
        # full of them.
        positional = [child for child in arguments.named_children if child.type != "comment"]
        literals = [_string_literal(child) for child in positional[:2]]
        if len(literals) == 2 and all(value is not None for value in literals):
            yield op, literals[0], literals[1]  # type: ignore[misc]


def _is_wot_object(node: Node | None) -> bool:
    if node is None:
        return False
    if node.type == "identifier":
        return _text(node) == "wot"
    if node.type == "member_expression":
        return _text(node.child_by_field_name("property")) == "wot" and _text(
            node.child_by_field_name("object")
        ) in ("window", "globalThis", "self")
    return False


def _string_literal(node: Node) -> str | None:
    """The value of a plain string literal, or None for anything else.

    Escapes disqualify it: decoding them correctly is JavaScript's job, and a
    thing id that needs one is not a case worth guessing at.
    """
    if node.type != "string":
        return None
    fragments = [child for child in node.children if child.type == "string_fragment"]
    if any(child.type == "escape_sequence" for child in node.children):
        return None
    if not fragments:
        return ""
    return _text(fragments[0])


def _is_allowed(
    declared: list[Mapping[str, Any]],
    op: str,
    thing_id: str,
    name: str,
) -> bool:
    # Mirrors isInteractionAllowed in web-interface-model.ts, including the
    # empty-affordance-list meaning "any affordance on this thing".
    for capability in declared:
        if capability["thingId"] != thing_id:
            continue
        if op not in (capability.get("ops") or []):
            continue
        affordances = capability.get("affordances") or []
        if not affordances or name in affordances:
            return True
    return False


def _egress_problems(parsed: list[tuple[PanelScript, Tree]]) -> list[str]:
    problems = []
    reported: set[str] = set()
    for _script, tree in parsed:
        for label in _blocked_egress(tree):
            if label in reported:
                continue
            reported.add(label)
            problems.append(
                f"The panel uses {label}, which the panel CSP blocks -- the call "
                "never completes and nothing explains why. Reach devices through "
                "window.wot instead."
            )
    return problems


def _blocked_egress(tree: Tree) -> Iterator[str]:
    for node in _walk(tree.root_node):
        if node.type == "new_expression":
            label = _BLOCKED_CONSTRUCTORS.get(_text(node.child_by_field_name("constructor")))
            if label:
                yield label
            continue
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None:
            continue
        if function.type == "identifier":
            label = _BLOCKED_FUNCTIONS.get(_text(function))
            if label:
                yield label
        elif function.type == "member_expression":
            name = _text(function.child_by_field_name("property"))
            label = _BLOCKED_METHODS.get(name)
            if label:
                yield label
            # `window.fetch(...)` is the same call by another name.
            elif name in _BLOCKED_FUNCTIONS and _text(function.child_by_field_name("object")) in (
                "window",
                "globalThis",
                "self",
            ):
                yield _BLOCKED_FUNCTIONS[name]
