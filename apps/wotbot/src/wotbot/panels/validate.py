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
string literals, and egress APIs the panel CSP blocks outright.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

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


@dataclass(frozen=True)
class PanelScript:
    """One executable ``<script>`` body and where it starts in the markup."""

    source: str
    first_line: int


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
    problems.extend(_syntax_problems(parsed))
    problems.extend(_capability_problems(declared, thing_affordances or {}))
    problems.extend(_bridge_call_problems(parsed, declared))
    problems.extend(_egress_problems(parsed))
    return problems


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
