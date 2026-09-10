from __future__ import annotations

import asyncio
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from pydantic import Field

from wotbot.agent.tools._config import thread_id_from_config as _thread_id_from_config
from wotbot.agent.tools.contracts import tool
from wotbot.core.time import utc_now
from wotbot.virtual_things.builder import (
    VirtualThingBuilder,
    action_definition,
    event_definition,
    event_trigger,
    property_definition,
)
from wotbot.virtual_things.dispatcher import VirtualThingDispatcher
from wotbot.virtual_things.store import VirtualThingStore


def get_virtual_thing_builder() -> VirtualThingBuilder:
    return VirtualThingBuilder()


def get_virtual_thing_store() -> VirtualThingStore:
    return VirtualThingStore()


def get_virtual_thing_dispatcher() -> VirtualThingDispatcher:
    return VirtualThingDispatcher()


@tool
async def create_virtual_thing(
    title: str,
    config: RunnableConfig,
    description: str = "",
    thing_id: str | None = None,
    shared_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a standalone virtual Thing. Returns its thing_id.

    The Thing is created `disabled` and empty. Use the returned thing_id with
    add_virtual_property, add_virtual_action, and add_virtual_event to add its
    affordances one at a time, then call activate_virtual_thing. Calling this
    again with the same thing_id returns the existing Thing unchanged so you can
    keep adding affordances. shared_state optionally seeds Thing-wide state
    available to every handler as context["shared_state"]; seed keys that
    handlers read directly with context["shared_state"]["key"]. To start over,
    delete_virtual_thing first.
    """
    builder = get_virtual_thing_builder()
    return await asyncio.to_thread(
        builder.create,
        title=title,
        description=description,
        thing_id=thing_id,
        owner_thread_id=_thread_id_from_config(config),
        shared_state=shared_state,
    )


@tool
async def add_virtual_property(
    thing_id: str,
    name: str,
    handler_code: str,
    value_schema: dict[str, Any] | None = None,
    cache_ttl_seconds: Annotated[int, Field(ge=0)] = 30,
) -> dict[str, Any]:
    """Add or replace a read-only computed property on a virtual Thing.

    handler_code is Python defining `def handle(input, state, context)` that
    returns the computed value. `state` is local to this property; use
    context["shared_state"] to read Thing-wide state. Read real Things inside
    handle with the injected `wot` client (wot.read_property / wot.invoke_action
    / wot.write_property); capability grants are inferred from literal
    thing_id/name strings.
    value_schema declares the computed value's JSON Schema in the published TD.
    Provide it whenever the value shape is known or requested: a numeric property
    needs {"type": "number"}, even if the user does not say "schema". For arrays
    declare items; for objects declare properties and required fields. Omit only
    for an unconstrained value. No schema is inferred from Python or smoke tests.
    cache_ttl_seconds caches each read for that many seconds (default 30) to
    avoid re-running the handler and re-hitting real Things on every read. Set it
    to 0 for properties that must recompute every read, e.g. ones returning
    random values or the current time; otherwise the first value is served
    unchanged until the TTL expires.
    """
    builder = get_virtual_thing_builder()
    return await asyncio.to_thread(
        builder.add_affordance,
        thing_id=thing_id,
        affordance_type="property",
        affordance_name=name,
        handler_code=handler_code,
        td_definition=property_definition(value_schema),
        cache_ttl_seconds=cache_ttl_seconds,
    )


@tool
async def add_virtual_action(
    thing_id: str,
    name: str,
    handler_code: str,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add or replace a computed action on a virtual Thing.

    handler_code is Python defining `def handle(input, state, context)` that
    returns the result. `input` is the action input. Mutate
    context["shared_state"] when the action should update Thing-wide state that
    properties or events can read later. Declare input_schema and output_schema
    for known or requested input/output shapes. A numeric action needs both
    {"type": "number"}; arrays need items and objects need properties/required.
    Omit a schema only when that value has no contract, e.g. an inputless action.
    Schemas are not inferred from Python or smoke tests.
    """
    builder = get_virtual_thing_builder()
    return await asyncio.to_thread(
        builder.add_affordance,
        thing_id=thing_id,
        affordance_type="action",
        affordance_name=name,
        handler_code=handler_code,
        td_definition=action_definition(input_schema, output_schema),
    )


@tool
async def add_virtual_event(
    thing_id: str,
    name: str,
    handler_code: str,
    interval_seconds: int | None = None,
    source_thing_id: str | None = None,
    source_event_name: str | None = None,
    data_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add or replace an emitted event on a virtual Thing.

    handler_code is Python defining `def handle(input, state, context)` that
    returns {"emit": bool, "payload": value, "state": next_state}. Use `state`
    for this event's threshold or edge detection; initialize it with
    `state = state or {}`. Mutate context["shared_state"] when the event should
    update Thing-wide state for other affordances.

    The trigger is set from these arguments:
    - interval_seconds=N evaluates the handler every N seconds.
    - source_thing_id + source_event_name re-evaluates on another Thing's event.
    - omit all three to make the event explicit (fire via emit_virtual_thing_event).
    data_schema declares the emitted payload, not the emit/payload/state wrapper.
    Provide it for known or requested payload shapes: numeric event data needs
    {"type": "number"}. Omit only for an unconstrained payload. No schema is
    inferred from Python or smoke tests, including tests that suppress emission.
    """
    builder = get_virtual_thing_builder()
    return await asyncio.to_thread(
        builder.add_affordance,
        thing_id=thing_id,
        affordance_type="event",
        affordance_name=name,
        handler_code=handler_code,
        td_definition=event_definition(data_schema),
        trigger=event_trigger(interval_seconds, source_thing_id, source_event_name),
    )


@tool
async def activate_virtual_thing(thing_id: str) -> dict[str, Any]:
    """Validate and activate a virtual Thing after its affordances are added.

    Runs a smoke test of every handler and, if it passes, flips the Thing from
    `disabled` to `active`. virtual-servient then produces the concrete catalog
    TD asynchronously. If a smoke test fails, fix the affordance with the
    matching add_virtual_* tool and call this again.
    """
    return await get_virtual_thing_builder().activate(thing_id)


@tool
async def delete_virtual_thing(thing_id: str) -> dict[str, Any]:
    """Delete a standalone virtual Thing definition by id."""
    try:
        await asyncio.to_thread(get_virtual_thing_store().delete_thing, thing_id)
    except KeyError:
        return {"error": "virtual thing not found"}
    except Exception as exc:
        return {"error": str(exc)}
    return {"ok": True, "thing_id": thing_id}


@tool
async def emit_virtual_thing_event(
    thing_id: str,
    event_name: str,
    input: Any = None,
) -> dict[str, Any]:
    """Evaluate and emit a standalone virtual Thing event with an explicit trigger."""
    try:
        return await get_virtual_thing_dispatcher().emit_event(
            thing_id,
            event_name,
            {
                "trigger": "explicit",
                "input": input,
                "requested_at": utc_now().isoformat(),
            },
        )
    except KeyError:
        return {"error": "virtual thing event not found"}
    except Exception as exc:
        return {"error": str(exc)}
