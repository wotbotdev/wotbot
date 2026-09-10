from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from wotbot.catalog.validation import validate_document
from wotbot.core.json import json_safe
from wotbot.virtual_things.capabilities import infer_capabilities
from wotbot.virtual_things.derivation import annotate_computed_derivations
from wotbot.virtual_things.ids import make_virtual_thing_id

AffordanceType = Literal["property", "action", "event"]
BindingKind = Literal["record", "computed", "emitted"]
VirtualThingStatus = Literal["active", "disabled"]

_WOT_TD_11_CONTEXT_URL = "https://www.w3.org/2022/wot/td/v1.1"


class VirtualThingCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thing_id: str
    affordances: list[str] = Field(default_factory=list)
    ops: list[str] = Field(default_factory=list)


class VirtualThingTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["interval", "source_event", "explicit"]
    interval_seconds: int | None = Field(default=None, ge=1)
    thing_id: str | None = None
    event_name: str | None = None
    subscription_input: Any | None = None

    @model_validator(mode="after")
    def _validate_trigger(self) -> VirtualThingTrigger:
        if self.kind == "interval" and self.interval_seconds is None:
            raise ValueError("interval triggers require interval_seconds")
        if self.kind == "source_event" and (not self.thing_id or not self.event_name):
            raise ValueError("source_event triggers require thing_id and event_name")
        return self


class VirtualThingBindingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    affordance_type: AffordanceType
    affordance_name: str
    kind: BindingKind
    handler_code: str | None = Field(
        default=None,
        validation_alias=AliasChoices("handler_code", "handle"),
    )
    config: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[VirtualThingCapability] = Field(default_factory=list)
    trigger: VirtualThingTrigger | None = None
    state: Any | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cache_ttl_seconds: int = Field(default=30, ge=0, le=3600)

    @field_validator("affordance_name")
    @classmethod
    def _name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("affordance_name is required")
        return value.strip()

    @field_validator("handler_code")
    @classmethod
    def _normalize_handler_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if _looks_like_javascript_handler(value):
            raise ValueError(
                "handler_code must be Python code that defines "
                "def handle(input, state, context), not JavaScript"
            )
        return value or None

    @model_validator(mode="before")
    @classmethod
    def _normalize_handler_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        _normalize_affordance_alias(normalized)
        _normalize_handler_code_alias(normalized)
        _normalize_kind_alias(normalized)
        _normalize_trigger_alias(normalized)
        return normalized

    @model_validator(mode="after")
    def _validate_binding_shape(self) -> VirtualThingBindingSpec:
        if self.kind in {"computed", "emitted"} and not self.handler_code:
            raise ValueError(f"{self.kind} bindings require handler_code")
        if self.kind == "emitted":
            if self.affordance_type != "event":
                raise ValueError("emitted bindings must bind events")
            if self.trigger is None:
                raise ValueError("emitted bindings require trigger")
        if self.kind == "computed" and self.affordance_type not in {"property", "action"}:
            raise ValueError("computed bindings only support properties and actions")
        return self

    @model_validator(mode="after")
    def _grant_inferred_capabilities(self) -> VirtualThingBindingSpec:
        inferred = infer_capabilities(self.handler_code)
        if inferred:
            self.capabilities = _merge_capabilities(self.capabilities, inferred)
        return self


class DefineVirtualThingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=180)
    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    td: dict[str, Any]
    bindings: list[VirtualThingBindingSpec]
    status: VirtualThingStatus = "active"
    owner_thread_id: str | None = Field(default=None, max_length=120)
    shared_state: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_request_shorthand(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        bindings = normalized.get("bindings")
        td = normalized.get("td")
        events = td.get("events") if isinstance(td, dict) else {}
        if isinstance(bindings, list):
            normalized["bindings"] = [
                _normalize_binding_from_td(binding, events) for binding in bindings
            ]
        return normalized

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> DefineVirtualThingRequest:
        thing_id = self.id or make_virtual_thing_id(self.title)
        td = {**self.td, "id": thing_id, "title": self.title}
        if self.description:
            td["description"] = self.description
        td.setdefault("@context", _WOT_TD_11_CONTEXT_URL)
        td = annotate_computed_derivations(td, self.bindings)
        self.id = thing_id
        self.td = validate_virtual_thing_td(td)
        _validate_binding_coverage(self.td, self.bindings)
        return self


class VirtualThingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    owner_thread_id: str | None = None
    td: dict[str, Any]
    version: int
    status: VirtualThingStatus
    shared_state: dict[str, Any] = Field(default_factory=dict)
    shared_state_version: int = 1
    bindings: list[VirtualThingBindingSpec]


class VirtualThingBindingView(BaseModel):
    """The slice of a binding that virtual-servient needs to expose a Thing.

    Deliberately omits ``handler_code``, ``capabilities``, ``config``, and
    ``state``: the servient never runs handlers (it delegates every interaction
    back to wotbot's dispatcher), so shipping handler source or capability
    grants to it would be needless exposure and wire weight.
    """

    model_config = ConfigDict(extra="forbid")

    affordance_type: AffordanceType
    affordance_name: str
    kind: BindingKind
    trigger: VirtualThingTrigger | None = None


class VirtualThingServientView(BaseModel):
    """Wire shape served to virtual-servient and the single source of truth for
    the wotbot <-> servient contract.

    ``apps/virtual-servient/src/types.generated.ts`` is generated from this
    model's JSON schema (see ``contract_export``); never hand-edit that file.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    td: dict[str, Any]
    version: int
    status: VirtualThingStatus
    bindings: list[VirtualThingBindingView]

    @classmethod
    def from_definition(cls, definition: VirtualThingDefinition) -> VirtualThingServientView:
        return cls(
            id=definition.id,
            title=definition.title,
            description=definition.description,
            td=definition.td,
            version=definition.version,
            status=definition.status,
            bindings=[
                VirtualThingBindingView(
                    affordance_type=binding.affordance_type,
                    affordance_name=binding.affordance_name,
                    kind=binding.kind,
                    trigger=binding.trigger,
                )
                for binding in definition.bindings
            ],
        )


def _normalize_affordance_alias(value: dict[str, Any]) -> None:
    if "affordance_name" not in value and "affordance" in value:
        value["affordance_name"] = value.pop("affordance")


def _normalize_handler_code_alias(value: dict[str, Any]) -> None:
    if "handler_code" in value:
        return
    for key in ("handle", "source", "code", "handler"):
        if key in value:
            value["handler_code"] = value.pop(key)
            return


def _normalize_kind_alias(value: dict[str, Any]) -> None:
    aliases = {
        "event": ("emitted", "event"),
        "action": ("computed", "action"),
        "property": ("computed", "property"),
    }
    alias = aliases.get(value.get("kind"))
    if alias is None:
        return
    kind, affordance_type = alias
    value["kind"] = kind
    value.setdefault("affordance_type", affordance_type)


def _normalize_trigger_alias(value: dict[str, Any]) -> None:
    if "trigger" in value:
        return
    if "interval_seconds" in value:
        value["trigger"] = {
            "kind": "interval",
            "interval_seconds": value.pop("interval_seconds"),
        }
        return
    if "evaluationInterval" not in value:
        return
    interval_ms = value.pop("evaluationInterval")
    if isinstance(interval_ms, (int, float)):
        value["trigger"] = {
            "kind": "interval",
            "interval_seconds": max(1, math.ceil(interval_ms / 1000)),
        }


def _merge_capabilities(
    explicit: list[VirtualThingCapability],
    inferred: list[dict[str, Any]],
) -> list[VirtualThingCapability]:
    """Union explicitly declared grants with statically inferred ones by thing_id.

    Empty ``affordances`` means "every affordance", so it dominates a specific
    list when the two are merged for the same Thing.
    """
    grants: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _accumulate(thing_id: str, ops: list[str], affordances: list[str]) -> None:
        grant = grants.get(thing_id)
        if grant is None:
            grant = {"ops": set(), "affordances": set(), "all_affordances": False}
            grants[thing_id] = grant
            order.append(thing_id)
        grant["ops"].update(ops)
        if affordances:
            grant["affordances"].update(affordances)
        else:
            grant["all_affordances"] = True

    for capability in explicit:
        _accumulate(capability.thing_id, capability.ops, capability.affordances)
    for capability in inferred:
        _accumulate(capability["thing_id"], capability["ops"], capability["affordances"])

    return [
        VirtualThingCapability(
            thing_id=thing_id,
            ops=sorted(grants[thing_id]["ops"]),
            affordances=(
                []
                if grants[thing_id]["all_affordances"]
                else sorted(grants[thing_id]["affordances"])
            ),
        )
        for thing_id in order
    ]


def validate_virtual_thing_td(td: Any) -> dict[str, Any]:
    if not isinstance(td, dict):
        raise ValueError("td must be an object")
    return json_safe(validate_document(_with_abstract_forms(td)))


def _with_abstract_forms(td: dict[str, Any]) -> dict[str, Any]:
    normalized = json_safe(td)
    normalized.setdefault("@context", _WOT_TD_11_CONTEXT_URL)
    normalized.setdefault("securityDefinitions", {"nosec_sc": {"scheme": "nosec"}})
    normalized.setdefault("security", "nosec_sc")
    for section, op in (
        ("properties", "readproperty"),
        ("actions", "invokeaction"),
        ("events", "subscribeevent"),
    ):
        affordances = normalized.get(section)
        if not isinstance(affordances, dict):
            continue
        for name, definition in affordances.items():
            if not isinstance(definition, dict):
                continue
            if section == "properties":
                # Virtual properties only have read handlers. node-wot otherwise
                # defaults to read/write when it generates the concrete forms.
                definition["readOnly"] = True
                definition["writeOnly"] = False
            if isinstance(definition.get("forms"), list) and definition["forms"]:
                continue
            definition["forms"] = [
                {
                    "href": f"urn:wotbot:virtual-things:{section}/{name}",
                    "op": [op],
                    "contentType": "application/json",
                }
            ]
    return normalized


def _validate_binding_coverage(
    td: dict[str, Any],
    bindings: list[VirtualThingBindingSpec],
) -> None:
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        key = (binding.affordance_type, binding.affordance_name)
        if key in seen:
            raise ValueError(
                f"duplicate binding for {binding.affordance_type} {binding.affordance_name}"
            )
        seen.add(key)
        section = {
            "property": "properties",
            "action": "actions",
            "event": "events",
        }[binding.affordance_type]
        affordances = td.get(section, {})
        if not isinstance(affordances, dict) or binding.affordance_name not in affordances:
            raise ValueError(
                f"binding references missing {binding.affordance_type} {binding.affordance_name!r}"
            )


def _looks_like_javascript_handler(value: str) -> bool:
    stripped = value.strip()
    return (
        stripped.startswith("function ")
        or stripped.startswith("return {")
        or "=>" in stripped
        or "??" in stripped
        or stripped.endswith("};")
    )


def _normalize_binding_from_td(binding: Any, events: Any) -> Any:
    if not isinstance(binding, dict):
        return binding
    normalized = dict(binding)
    affordance = normalized.get("affordance_name") or normalized.get("affordance")
    event_definition = (
        events.get(affordance) if isinstance(events, dict) and isinstance(affordance, str) else None
    )
    if (
        "trigger" not in normalized
        and isinstance(event_definition, dict)
        and isinstance(event_definition.get("evaluationInterval"), (int, float))
    ):
        normalized["evaluationInterval"] = event_definition["evaluationInterval"]
    return normalized
