from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from wotbot.clients.code_executor import CodeExecutionUncertainError, CodeExecutorClient
from wotbot.virtual_things.schemas import json_safe

logger = logging.getLogger(__name__)

RESULT_PREFIX = "__VIRTUAL_THING_RESULT__"
HANDLER_RESULT_VERSION = 1
_HANDLER_RESULT_VERSION_KEY = "__virtual_thing_result_version"
# Bytes of randomness in the per-run result token. The token makes the result
# marker unguessable, so handler stdout (even a handler that prints RESULT_PREFIX
# itself) cannot forge or collide with the envelope line.
_RESULT_TOKEN_BYTES = 16


def result_marker(result_token: str) -> str:
    """The exact line prefix that tags this run's result envelope on stdout."""
    return f"{RESULT_PREFIX}{result_token}:"


def decode_result_envelope(stdout: str, result_token: str) -> tuple[bool, Any]:
    """Find and decode this run's result envelope on ``stdout``.

    Returns ``(found, value)``. The handler wrapper prints ``<marker><base64(json)>``
    on its own line; we scan bottom-up for the line carrying this run's marker and
    base64-decode it, so arbitrary handler ``print`` output (multi-line, or even a
    line that starts with ``RESULT_PREFIX``) cannot be mistaken for the result.
    ``found`` distinguishes "no result line" from a result whose value is JSON
    ``null``.
    """
    marker = result_marker(result_token)
    for line in reversed(stdout.splitlines()):
        if not line.startswith(marker):
            continue
        encoded = line[len(marker) :]
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        return True, json.loads(decoded.decode("utf-8"))
    return False, None


class HandlerBinding(Protocol):
    id: str
    thing_id: str
    affordance_type: str
    affordance_name: str
    handler_code: str | None
    capabilities: Any
    config: Any
    timeout_seconds: int


class VirtualThingHandlerError(RuntimeError):
    """Raised when virtual handler code fails during execution."""


@dataclass(frozen=True)
class HandlerRunResult:
    value: Any
    state: Any
    shared_state: dict[str, Any]


@dataclass(frozen=True)
class HandlerContext:
    thing_id: str
    affordance_type: str
    affordance_name: str
    capabilities: list[dict[str, Any]]
    config: dict[str, Any]
    shared_state: dict[str, Any]
    shared_state_version: int


class VirtualThingHandlerRunner:
    """Runs computed and emitted virtual Thing handler code."""

    def __init__(self, code_executor: CodeExecutorClient) -> None:
        self._code_executor = code_executor

    async def run_handler(
        self,
        binding: HandlerBinding,
        *,
        input_value: Any,
        state: Any,
        shared_state: dict[str, Any] | None = None,
        shared_state_version: int = 1,
    ) -> HandlerRunResult:
        if not binding.handler_code:
            raise ValueError("binding has no handler_code")
        context = HandlerContext(
            thing_id=binding.thing_id,
            affordance_type=binding.affordance_type,
            affordance_name=binding.affordance_name,
            capabilities=list(binding.capabilities or []),
            config=dict(binding.config or {}),
            shared_state=dict(shared_state or {}),
            shared_state_version=shared_state_version,
        )
        initial_state = {} if state is None else state
        result_token = secrets.token_hex(_RESULT_TOKEN_BYTES)
        code = handler_wrapper(
            handler_code=binding.handler_code,
            input_value=input_value,
            state=initial_state,
            context=context,
            result_token=result_token,
        )
        executor_started = time.perf_counter()
        try:
            response = await self._code_executor.execute(
                session_id=f"virtual-thing:{binding.id}",
                code=code,
                timeout_seconds=getattr(binding, "timeout_seconds", None),
            )
        except CodeExecutionUncertainError as exc:
            raise VirtualThingHandlerError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError("computed handler timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise VirtualThingHandlerError(
                f"computed handler failed with status {exc.response.status_code}"
            ) from exc

        logger.debug(
            "virtual handler %s/%s executor_ms=%.1f",
            binding.thing_id,
            binding.affordance_name,
            (time.perf_counter() - executor_started) * 1000,
        )
        stdout = str(response.get("stdout", ""))
        if response.get("ok") is False:
            raise VirtualThingHandlerError(
                str(response.get("error") or stdout.strip() or "computed handler failed")
            )
        found, payload = decode_result_envelope(stdout, result_token)
        if not found:
            raise VirtualThingHandlerError(
                stdout.strip() or "computed handler did not return a result"
            )
        return _parse_handler_result(
            payload,
            state=initial_state,
            shared_state=context.shared_state,
        )


def handler_wrapper(
    *,
    handler_code: str,
    input_value: Any,
    state: Any,
    context: HandlerContext,
    result_token: str,
) -> str:
    payload = {
        "input": json_safe(input_value),
        "state": json_safe(state),
        "context": {
            "thing_id": context.thing_id,
            "affordance_type": context.affordance_type,
            "affordance_name": context.affordance_name,
            "capabilities": context.capabilities,
            "config": context.config,
            "shared_state": context.shared_state,
            "shared_state_version": context.shared_state_version,
        },
    }
    payload_json = json.dumps(payload, ensure_ascii=True)
    result_marker_json = json.dumps(result_marker(result_token))
    return f"""
import base64 as __vt_b64
import json as __vt_json
import sys as __vt_sys
import types as __vt_types

__vt_payload = __vt_json.loads({payload_json!r})
__vt_input = __vt_payload["input"]
__vt_state = __vt_payload["state"]
__vt_context = __vt_payload["context"]
__vt_caps = __vt_context.get("capabilities") or []
# Single-underscore names: identifiers referenced inside the guard class must not
# start with ``__`` or Python name-mangling rewrites them (e.g. ``__vt_check`` ->
# ``_VtGuardedWot__vt_check``), which raises NameError the first time a handler
# actually calls a capability.
_vt_real_wot = getattr(wot, "_vt_real_wot", wot)

def _vt_check(op, thing_id, name):
    for cap in __vt_caps:
        if cap.get("thing_id") != thing_id:
            continue
        if op not in (cap.get("ops") or []):
            continue
        affordances = cap.get("affordances") or []
        if not affordances or name in affordances:
            return
    raise PermissionError(f"Virtual Thing handler is not allowed to {{op}} {{thing_id}}/{{name}}")

class _VtGuardedWot:
    _vt_is_guarded_wot = True

    def __init__(self, real_wot):
        self._vt_real_wot = real_wot

    def read_property(self, thing_id, property_name, uri_variables=None):
        _vt_check("readProperty", thing_id, property_name)
        return self._vt_real_wot.read_property(thing_id, property_name, uri_variables)

    def write_property(self, thing_id, property_name, value, uri_variables=None):
        _vt_check("writeProperty", thing_id, property_name)
        return self._vt_real_wot.write_property(thing_id, property_name, value, uri_variables)

    def invoke_action(self, thing_id, action_name, input=None, uri_variables=None):
        _vt_check("invokeAction", thing_id, action_name)
        return self._vt_real_wot.invoke_action(thing_id, action_name, input, uri_variables)

wot = _VtGuardedWot(_vt_real_wot)
__vt_wot_module = __vt_types.ModuleType("wot")
__vt_wot_module.read_property = wot.read_property
__vt_wot_module.write_property = wot.write_property
__vt_wot_module.invoke_action = wot.invoke_action
__vt_sys.modules["wot"] = __vt_wot_module

{handler_code}

if "handle" not in globals() or not callable(handle):
    raise RuntimeError("Virtual Thing handler_code must define handle(input, state, context)")

__vt_result = handle(__vt_input, __vt_state, __vt_context)
__vt_envelope = {{
    {_HANDLER_RESULT_VERSION_KEY!r}: {HANDLER_RESULT_VERSION},
    "value": __vt_result,
    "state": __vt_state,
    "shared_state": __vt_context.get("shared_state") or {{}},
}}
__vt_encoded = __vt_b64.b64encode(
    __vt_json.dumps(__vt_envelope, ensure_ascii=True, default=str).encode("utf-8")
).decode("ascii")
print({result_marker_json} + __vt_encoded)
"""


def _parse_handler_result(
    payload: Any,
    *,
    state: Any,
    shared_state: dict[str, Any],
) -> HandlerRunResult:
    safe_payload = json_safe(payload)
    if (
        isinstance(safe_payload, dict)
        and safe_payload.get(_HANDLER_RESULT_VERSION_KEY) == HANDLER_RESULT_VERSION
    ):
        return HandlerRunResult(
            value=safe_payload.get("value"),
            state=safe_payload.get("state"),
            shared_state=_normalize_shared_state(safe_payload.get("shared_state")),
        )
    return HandlerRunResult(
        value=safe_payload,
        state=json_safe(state),
        shared_state=_normalize_shared_state(shared_state),
    )


def _normalize_shared_state(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VirtualThingHandlerError("context.shared_state must be a JSON object")
    safe_value = json_safe(value)
    if not isinstance(safe_value, dict):
        raise VirtualThingHandlerError("context.shared_state must be a JSON object")
    return safe_value
