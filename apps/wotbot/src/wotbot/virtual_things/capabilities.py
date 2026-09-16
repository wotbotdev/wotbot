"""Infer source-Thing capability grants from virtual Thing handler code.

A handler reaches real Things through the injected ``wot`` client:

    wot.read_property(thing_id, property_name)
    wot.write_property(thing_id, property_name, value)
    wot.invoke_action(thing_id, action_name, input)

The runtime guard rejects any call that is not pre-declared in the binding's
``capabilities``. Authoring those grants by hand is the step LLMs most often get
wrong, so we derive them statically from the handler's ``wot`` calls and union
them with anything the author declared explicitly.

Arguments are resolved through simple local constants and ``for`` loops over
literal collections, so the common pattern of collecting sensors in a list and
looping over it grants the same scoped capabilities as spelling every call out
with literal strings::

    SENSORS = [("urn:a", "temp"), ("urn:b", "temp")]
    for tid, prop in SENSORS:
        wot.read_property(tid, prop)

Anything that cannot be reduced to literal strings (e.g. a thing_id read from
``context`` or ``input``) stays unscopable: no grant is inferred and the runtime
guard blocks it unless the author declares the capability explicitly.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

_OP_BY_METHOD = {
    "read_property": "readProperty",
    "write_property": "writeProperty",
    "invoke_action": "invokeAction",
}
_THING_KEYWORDS = ("thing_id",)
_NAME_KEYWORDS = ("property_name", "action_name", "name")

# Marks an argument/name that is present but does not reduce to a literal value.
_DYNAMIC = object()
# Sentinel for "no value assigned yet" while reconciling repeated assignments.
_UNSET = object()
# Cap loop unrolling so a pathologically large literal list cannot blow up the
# static analysis; beyond this the loop targets are treated as dynamic.
_MAX_UNROLL_ROWS = 256

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def infer_capabilities(handler_code: str | None) -> list[dict[str, Any]]:
    """Return capability grants implied by literal ``wot`` calls in the handler."""
    grants: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for thing_id, op, name, _method in _wot_calls(handler_code):
        if not isinstance(thing_id, str):
            # Dynamic or missing thing_id cannot be scoped; author must declare it.
            continue
        _merge_capability_grant(grants, order, (thing_id, op, name))

    return [
        {
            "thing_id": thing_id,
            "ops": sorted(grants[thing_id]["ops"]),
            "affordances": (
                []
                if grants[thing_id]["all_affordances"]
                else sorted(grants[thing_id]["affordances"])
            ),
        }
        for thing_id in order
    ]


def find_unscopable_wot_calls(handler_code: str | None) -> list[str]:
    """Return sorted ``wot`` method names called with a non-literal thing_id.

    These calls cannot have a capability grant inferred, so the runtime guard
    blocks them unless the author declares an explicit capability. Surfacing them
    lets validation fail loudly instead of the handler silently hitting
    ``PermissionError`` at runtime.
    """
    methods = {
        method
        for thing_id, _op, _name, method in _wot_calls(handler_code)
        if not isinstance(thing_id, str)
    }
    return sorted(methods)


def _wot_calls(handler_code: str | None) -> list[tuple[Any, str, Any, str]]:
    """Return ``(thing_id, op, name, method)`` for every ``wot`` call.

    ``thing_id``/``name`` are literal strings when resolvable, otherwise
    ``_DYNAMIC``. Loops over literal collections are unrolled so each iteration
    contributes its own resolved call.
    """
    if not handler_code:
        return []
    try:
        tree = ast.parse(handler_code)
    except SyntaxError:
        return []
    collector = _WotCallCollector(_build_const_resolver(tree))
    collector.visit_block(tree.body, {})
    return collector.calls


def _build_const_resolver(tree: ast.AST) -> Callable[[str], Any]:
    """Resolve module/function-level constant assignments to literal values."""
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)

    cache: dict[str, Any] = {}

    def resolve(name: str, stack: tuple[str, ...] = ()) -> Any:
        if name in cache:
            return cache[name]
        exprs = assignments.get(name)
        if not exprs or name in stack:
            return _DYNAMIC
        lookup = lambda inner: resolve(inner, stack + (name,))
        value: Any = _UNSET
        for expr in exprs:
            ok, resolved = _eval_literal(expr, lookup)
            if not ok:
                value = _DYNAMIC
                break
            if value is _UNSET:
                value = resolved
            elif value != resolved:
                # Reassigned to a different constant; cannot pick one soundly.
                value = _DYNAMIC
                break
        cache[name] = _DYNAMIC if value is _UNSET else value
        return cache[name]

    return resolve


class _WotCallCollector:
    """Walks handler code, unrolling literal loops to resolve ``wot`` call args."""

    def __init__(self, resolve_const: Callable[[str], Any]) -> None:
        self._resolve_const = resolve_const
        self.calls: list[tuple[Any, str, Any, str]] = []

    def visit_block(self, stmts: list[ast.stmt], env: dict[str, Any]) -> None:
        for stmt in stmts:
            self.visit(stmt, env)

    def visit(self, node: ast.AST, env: dict[str, Any]) -> None:
        if isinstance(node, ast.For):
            self._visit_for(node, env)
            return
        if isinstance(node, _COMPREHENSIONS):
            self._visit_comprehension(node, env)
            return
        if isinstance(node, ast.Call) and _is_wot_call(node):
            self._record_call(node, env)
        for child in ast.iter_child_nodes(node):
            self.visit(child, env)

    def _visit_for(self, node: ast.For, env: dict[str, Any]) -> None:
        rows = self._resolve_iterable(node.iter, env)
        if rows is None or len(rows) > _MAX_UNROLL_ROWS:
            # Unknown/too-large iterable: loop targets stay unbound (dynamic).
            self.visit_block(node.body, env)
            self.visit_block(node.orelse, env)
            return
        for row in rows:
            scoped = dict(env)
            body_env = scoped if _bind_target(node.target, row, scoped) else env
            self.visit_block(node.body, body_env)
        self.visit_block(node.orelse, env)

    def _visit_comprehension(self, node: ast.expr, env: dict[str, Any]) -> None:
        if isinstance(node, ast.DictComp):
            elements = [node.key, node.value]
        else:
            elements = [node.elt]  # type: ignore[attr-defined]
        self._unroll_generators(node.generators, 0, env, elements)  # type: ignore[attr-defined]

    def _unroll_generators(
        self,
        generators: list[ast.comprehension],
        index: int,
        env: dict[str, Any],
        elements: list[ast.expr],
    ) -> None:
        if index == len(generators):
            for element in elements:
                self.visit(element, env)
            return
        generator = generators[index]
        rows = self._resolve_iterable(generator.iter, env)
        if rows is None or len(rows) > _MAX_UNROLL_ROWS:
            # Unknown iterable: target stays unbound (dynamic) for the rest.
            self.visit_block(generator.ifs, env)
            self._unroll_generators(generators, index + 1, env, elements)
            return
        for row in rows:
            scoped = dict(env)
            body_env = scoped if _bind_target(generator.target, row, scoped) else env
            self.visit_block(generator.ifs, body_env)
            self._unroll_generators(generators, index + 1, body_env, elements)

    def _resolve_iterable(self, node: ast.expr, env: dict[str, Any]) -> list[Any] | None:
        ok, value = _eval_literal(node, lambda name: self._lookup(env, name))
        if ok and isinstance(value, (list, tuple)):
            return list(value)
        return None

    def _record_call(self, node: ast.Call, env: dict[str, Any]) -> None:
        method = node.func.attr  # type: ignore[attr-defined]
        op = _OP_BY_METHOD[method]
        thing_id = self._string_arg(node, 0, _THING_KEYWORDS, env)
        name = self._string_arg(node, 1, _NAME_KEYWORDS, env)
        self.calls.append((thing_id, op, name, method))

    def _string_arg(
        self,
        node: ast.Call,
        index: int,
        keywords: tuple[str, ...],
        env: dict[str, Any],
    ) -> Any:
        expr = _positional_or_keyword(node, index, keywords)
        if expr is None:
            return _DYNAMIC
        ok, value = _eval_literal(expr, lambda name: self._lookup(env, name))
        return value if ok and isinstance(value, str) else _DYNAMIC

    def _lookup(self, env: dict[str, Any], name: str) -> Any:
        if name in env:
            return env[name]
        return self._resolve_const(name)


def _is_wot_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "wot"
        and func.attr in _OP_BY_METHOD
    )


def _positional_or_keyword(
    node: ast.Call,
    index: int,
    keywords: tuple[str, ...],
) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    for keyword in node.keywords:
        if keyword.arg in keywords:
            return keyword.value
    return None


def _bind_target(target: ast.expr, value: Any, env: dict[str, Any]) -> bool:
    if isinstance(target, ast.Name):
        env[target.id] = value
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (list, tuple)) or len(value) != len(target.elts):
            return False
        return all(_bind_target(element, item, env) for element, item in zip(target.elts, value))
    return False


def _eval_literal(node: ast.expr, lookup: Callable[[str], Any]) -> tuple[bool, Any]:
    """Reduce an expression to a literal value using ``lookup`` for names."""
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, ast.Name):
        value = lookup(node.id)
        return (False, None) if value is _DYNAMIC else (True, value)
    if isinstance(node, (ast.List, ast.Tuple)):
        items: list[Any] = []
        for element in node.elts:
            ok, value = _eval_literal(element, lookup)
            if not ok:
                return False, None
            items.append(value)
        return True, items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        ok, value = _eval_literal(node.operand, lookup)
        if ok and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True, -value
    return False, None


def _merge_capability_grant(
    grants: dict[str, dict[str, Any]],
    order: list[str],
    capability: tuple[str, str, Any],
) -> None:
    thing_id, op, name = capability
    grant = grants.get(thing_id)
    if grant is None:
        grant = {"ops": set(), "affordances": set(), "all_affordances": False}
        grants[thing_id] = grant
        order.append(thing_id)
    grant["ops"].add(op)
    if isinstance(name, str):
        grant["affordances"].add(name)
    else:
        # Dynamic/absent affordance name -> grant every affordance on this Thing.
        grant["all_affordances"] = True
