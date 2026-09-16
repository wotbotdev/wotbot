from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from wotbot.clients.code_executor import CodeExecutorClient
from wotbot.core.settings import Settings
from wotbot.virtual_things.cache import get_cached_value, set_cached_value
from wotbot.virtual_things.handler import (
    RESULT_PREFIX,
    HandlerRunResult,
    VirtualThingHandlerRunner,
)
from wotbot.virtual_things.schemas import json_safe
from wotbot.virtual_things.store import VirtualThingStore

if TYPE_CHECKING:
    from wotbot.jobs.records import VirtualRecordStore

logger = logging.getLogger(__name__)

_RESULT_PREFIX = RESULT_PREFIX


class VirtualThingDispatcher:
    """Dispatches virtual affordance interactions by binding kind."""

    def __init__(
        self,
        *,
        store: VirtualThingStore | None = None,
        record_store: VirtualRecordStore | None = None,
        code_executor: CodeExecutorClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        from wotbot.jobs.records import VirtualRecordStore

        self._store = store or VirtualThingStore()
        self._record_store = record_store or VirtualRecordStore()
        self._settings = settings or Settings()
        self._code_executor = code_executor or CodeExecutorClient(self._settings)
        self._handler_runner = VirtualThingHandlerRunner(self._code_executor)

    async def read_property(self, thing_id: str, property_name: str) -> Any:
        binding = self._store.get_runtime_binding(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name=property_name,
        )
        if binding.kind == "record":
            return self._record_store.read_property(thing_id, property_name)
        if binding.kind != "computed":
            raise KeyError(property_name)

        cache_key = f"{thing_id}:property:{property_name}:shared:{binding.shared_state_version}"
        cached = get_cached_value(cache_key)
        if cached.found:
            logger.debug("virtual read %s/%s cache=hit", thing_id, property_name)
            return cached.value

        started = time.perf_counter()
        result = await self._handler_runner.run_handler(
            binding,
            input_value=None,
            state=binding.state,
            shared_state=binding.shared_state,
            shared_state_version=binding.shared_state_version,
        )
        logger.debug(
            "virtual read %s/%s cache=miss handler_ms=%.1f",
            thing_id,
            property_name,
            (time.perf_counter() - started) * 1000,
        )
        if binding.cache_ttl_seconds > 0:
            set_cached_value(cache_key, result.value, binding.cache_ttl_seconds)
        return result.value

    async def invoke_action(self, thing_id: str, action_name: str, input_data: Any) -> Any:
        binding = self._store.get_runtime_binding(
            thing_id=thing_id,
            affordance_type="action",
            affordance_name=action_name,
        )
        if binding.kind == "record":
            return self._record_store.invoke_action(thing_id, action_name, input_data)
        if binding.kind != "computed":
            raise KeyError(action_name)
        result = await self._handler_runner.run_handler(
            binding,
            input_value=input_data,
            state=binding.state,
            shared_state=binding.shared_state,
            shared_state_version=binding.shared_state_version,
        )
        if _shared_state_changed(binding.shared_state, result):
            self._store.update_shared_state(
                thing_id=thing_id,
                expected_version=binding.shared_state_version,
                shared_state=result.shared_state,
            )
        return result.value

    async def evaluate_event(
        self,
        thing_id: str,
        event_name: str,
        trigger_input: Any,
        *,
        dry_run: bool = False,
    ) -> Any | None:
        result = await self._evaluate_event_result(
            thing_id,
            event_name,
            trigger_input,
            dry_run=dry_run,
        )
        return result["payload"] if result["emitted"] else None

    async def emit_event(
        self,
        thing_id: str,
        event_name: str,
        trigger_input: Any,
    ) -> dict[str, Any]:
        result = await self._evaluate_event_result(
            thing_id,
            event_name,
            trigger_input,
            dry_run=False,
        )
        if result["emitted"]:
            self._store.enqueue_event_emission(
                thing_id=thing_id,
                event_name=event_name,
                payload=result.get("payload"),
            )
        return result

    async def _evaluate_event_result(
        self,
        thing_id: str,
        event_name: str,
        trigger_input: Any,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        binding = self._store.get_runtime_binding(
            thing_id=thing_id,
            affordance_type="event",
            affordance_name=event_name,
        )
        if binding.kind != "emitted":
            raise KeyError(event_name)
        result = await self._handler_runner.run_handler(
            binding,
            input_value=trigger_input,
            state=binding.state,
            shared_state=binding.shared_state,
            shared_state_version=binding.shared_state_version,
        )
        _validate_event_result(event_name, result.value)
        if not dry_run:
            shared_state_changed = _shared_state_changed(binding.shared_state, result)
            if shared_state_changed:
                self._store.update_binding_and_shared_state(
                    binding_id=binding.id,
                    state=result.value.get("state"),
                    thing_id=thing_id,
                    expected_shared_state_version=binding.shared_state_version,
                    shared_state=result.shared_state,
                    update_shared_state=True,
                )
            else:
                self._store.update_binding_state(
                    binding_id=binding.id,
                    state=result.value.get("state"),
                )
        emitted = result.value["emit"]
        return {
            "thing_id": thing_id,
            "event_name": event_name,
            "emitted": emitted,
            "payload": result.value.get("payload") if emitted else None,
        }


def _validate_event_result(event_name: str, result: Any) -> None:
    if result is None:
        raise ValueError(
            f"event {event_name!r} handler returned None. Return an object with emit, payload, "
            "and state."
        )
    if not isinstance(result, dict):
        raise ValueError(f"event {event_name!r} handlers must return an object")
    if not isinstance(result.get("emit"), bool):
        raise ValueError(f"event {event_name!r} result.emit must be a boolean")
    if "state" not in result:
        raise ValueError(f"event {event_name!r} result.state is required")
    if result["emit"] and "payload" not in result:
        raise ValueError(f"event {event_name!r} result.payload is required when emit=true")


def _shared_state_changed(
    original: dict[str, Any],
    result: HandlerRunResult,
) -> bool:
    return json_safe(original or {}) != json_safe(result.shared_state or {})
