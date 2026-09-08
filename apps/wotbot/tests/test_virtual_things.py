import base64
import contextlib
import io
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wotbot.virtual_things.builder import (
    VirtualThingBuilder,
    action_definition,
    event_definition,
    event_trigger,
    property_definition,
)
from wotbot.virtual_things.capabilities import find_unscopable_wot_calls, infer_capabilities
from wotbot.virtual_things.dispatcher import _RESULT_PREFIX, VirtualThingDispatcher
from wotbot.virtual_things.schemas import (
    DefineVirtualThingRequest,
    VirtualThingBindingSpec,
    VirtualThingDefinition,
)
from wotbot.virtual_things.store import VirtualThingStateConflict
from wotbot.virtual_things.validator import VirtualThingValidator


def _base_td() -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        "properties": {"temperature": {"type": "number"}},
        "actions": {"refresh": {"input": {"type": "object"}, "output": {"type": "number"}}},
        "events": {"threshold_crossed": {"data": {"type": "object"}}},
    }


class _FakeStore:
    def __init__(self, binding):
        self.binding = binding
        self.updated_state = None
        self.updated_shared_state = None
        self.enqueued_emission = None

    def get_binding(self, **_kwargs):
        return self.binding

    def get_runtime_binding(self, **_kwargs):
        if not hasattr(self.binding, "shared_state"):
            self.binding.shared_state = {}
        if not hasattr(self.binding, "shared_state_version"):
            self.binding.shared_state_version = 1
        return self.binding

    def update_binding_state(self, *, binding_id, state):
        self.updated_state = (binding_id, state)

    def update_shared_state(self, *, thing_id, expected_version, shared_state):
        self.updated_shared_state = (thing_id, expected_version, shared_state)
        return expected_version + 1

    def update_binding_and_shared_state(
        self,
        *,
        binding_id,
        state,
        thing_id,
        expected_shared_state_version,
        shared_state,
        update_shared_state,
    ):
        self.updated_state = (binding_id, state)
        if update_shared_state:
            self.updated_shared_state = (
                thing_id,
                expected_shared_state_version,
                shared_state,
            )
            return expected_shared_state_version + 1
        return None

    def enqueue_event_emission(self, *, thing_id, event_name, payload):
        self.enqueued_emission = (thing_id, event_name, payload)


class _SharedStateStore:
    def __init__(self, bindings, shared_state=None):
        self.bindings = bindings
        self.shared_state = dict(shared_state or {})
        self.shared_state_version = 1
        self.reject_shared_updates = False

    def get_runtime_binding(self, *, affordance_type, affordance_name, **_kwargs):
        binding = self.bindings[(affordance_type, affordance_name)]
        binding.shared_state = dict(self.shared_state)
        binding.shared_state_version = self.shared_state_version
        return binding

    def update_shared_state(self, *, thing_id, expected_version, shared_state):
        if self.reject_shared_updates or expected_version != self.shared_state_version:
            raise VirtualThingStateConflict(f"shared state for {thing_id!r} changed")
        self.shared_state = dict(shared_state)
        self.shared_state_version += 1
        return self.shared_state_version

    def update_binding_and_shared_state(
        self,
        *,
        binding_id,
        state,
        thing_id,
        expected_shared_state_version,
        shared_state,
        update_shared_state,
    ):
        binding = next(item for item in self.bindings.values() if item.id == binding_id)
        binding.state = state
        if not update_shared_state:
            return None
        return self.update_shared_state(
            thing_id=thing_id,
            expected_version=expected_shared_state_version,
            shared_state=shared_state,
        )


_MARKER_RE = re.compile(rf"{re.escape(_RESULT_PREFIX)}[0-9a-f]+:")


class _FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.kwargs = []

    async def execute(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        result = self.results.pop(0)
        # Mimic a real executor: echo the run's token-tagged, base64-encoded
        # result marker that the handler wrapper would have printed. The marker
        # (including the per-run token) is embedded in the generated ``code``.
        match = _MARKER_RE.search(kwargs.get("code", ""))
        marker = match.group(0) if match else _RESULT_PREFIX
        encoded = base64.b64encode(result.encode("utf-8")).decode("ascii")
        return {"stdout": f"{marker}{encoded}"}


class _LocalExecutor:
    async def execute(self, *, session_id, code, timeout_seconds=None):
        stdout = io.StringIO()
        namespace = {
            "wot": SimpleNamespace(
                read_property=lambda *_args, **_kwargs: None,
                write_property=lambda *_args, **_kwargs: None,
                invoke_action=lambda *_args, **_kwargs: None,
            )
        }
        with contextlib.redirect_stdout(stdout):
            exec(code, namespace, namespace)
        return {"stdout": stdout.getvalue()}


class _FakeDefinitionStore:
    def __init__(self):
        self.things = {}
        self.requests = []

    def get_definition(self, thing_id, *, include_disabled=False):
        definition = self.things.get(thing_id)
        if definition is None or (definition.status != "active" and not include_disabled):
            raise KeyError(thing_id)
        return definition

    def define_thing(self, request):
        self.requests.append(request)
        previous = self.things.get(request.id)
        definition = VirtualThingDefinition(
            id=request.id,
            title=request.title,
            description=request.description,
            owner_thread_id=request.owner_thread_id,
            td=request.td,
            version=(previous.version + 1) if previous else 1,
            status=request.status,
            shared_state=(
                request.shared_state
                if request.shared_state is not None
                else (previous.shared_state if previous else {})
            ),
            shared_state_version=(
                (previous.shared_state_version + 1)
                if previous and request.shared_state is not None
                else (previous.shared_state_version if previous else 1)
            ),
            bindings=request.bindings,
        )
        self.things[request.id] = definition
        return definition


class VirtualThingBuilderTestCase(unittest.TestCase):
    def _builder(self):
        store = _FakeDefinitionStore()
        return VirtualThingBuilder(store=store, validator=VirtualThingValidator()), store

    def test_create_starts_disabled_and_empty(self):
        builder, store = self._builder()
        result = builder.create(title="Grid Headroom", description="Says hi")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["virtual_thing"]["status"], "disabled")
        self.assertEqual(result["virtual_thing"]["bindings"], [])
        self.assertEqual(store.requests[0].status, "disabled")

    def test_create_can_seed_shared_state(self):
        builder, store = self._builder()

        result = builder.create(title="Controller", shared_state={"mode": "eco"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["virtual_thing"]["shared_state"], {"mode": "eco"})
        self.assertEqual(store.requests[0].shared_state, {"mode": "eco"})

    def test_create_is_idempotent_and_keeps_existing_affordances(self):
        builder, _ = self._builder()
        thing_id = "virtual:things:comfort-abc12345"
        builder.create(title="Comfort", thing_id=thing_id)
        builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="score",
            handler_code="def handle(input, state, context):\n    return 72",
            td_definition=property_definition({"type": "number"}),
        )
        again = builder.create(title="Comfort", thing_id=thing_id)
        self.assertTrue(again.get("existing"))
        self.assertEqual(len(again["virtual_thing"]["bindings"]), 1)

    def test_add_affordance_preserves_shared_state(self):
        builder, store = self._builder()
        thing_id = builder.create(
            title="Controller",
            shared_state={"mode": "eco"},
        )["thing_id"]

        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="mode",
            handler_code="def handle(input, state, context):\n    return 'eco'",
            td_definition=property_definition({"type": "string"}),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            store.get_definition(thing_id, include_disabled=True).shared_state, {"mode": "eco"}
        )

    def test_add_affordance_sets_cache_ttl_seconds(self):
        builder, store = self._builder()
        thing_id = builder.create(title="Sampler")["thing_id"]

        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="dice",
            handler_code="def handle(input, state, context):\n    return 4",
            td_definition=property_definition({"type": "number"}),
            cache_ttl_seconds=0,
        )

        self.assertTrue(result["ok"], result)
        binding = store.get_definition(thing_id, include_disabled=True).bindings[0]
        self.assertEqual(binding.cache_ttl_seconds, 0)

    def test_add_affordance_defaults_cache_ttl_seconds(self):
        builder, store = self._builder()
        thing_id = builder.create(title="Sampler")["thing_id"]

        builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="score",
            handler_code="def handle(input, state, context):\n    return 72",
            td_definition=property_definition({"type": "number"}),
        )

        binding = store.get_definition(thing_id, include_disabled=True).bindings[0]
        self.assertEqual(binding.cache_ttl_seconds, 30)

    def test_concurrent_add_affordance_keeps_all(self):
        # Regression: parallel add_virtual_* tool calls each construct their own builder
        # and run in separate threads (asyncio.to_thread). add_affordance is a
        # read-modify-write, so without per-Thing serialization concurrent adds clobber
        # each other and affordances silently disappear before activation.
        import time
        from concurrent.futures import ThreadPoolExecutor

        class _SlowStore(_FakeDefinitionStore):
            def define_thing(self, request):
                time.sleep(0.02)  # widen the read-modify-write window for the race
                return super().define_thing(request)

        store = _SlowStore()
        validator = VirtualThingValidator()
        thing_id = "virtual:things:multi-abc12345"
        VirtualThingBuilder(store=store, validator=validator).create(
            title="Multi", thing_id=thing_id
        )

        specs = [("property", f"p{i}") for i in range(3)] + [("action", f"a{i}") for i in range(3)]

        def add(spec):
            affordance_type, name = spec
            td_definition = (
                property_definition({"type": "number"})
                if affordance_type == "property"
                else action_definition(None, {"type": "number"})
            )
            return VirtualThingBuilder(store=store, validator=validator).add_affordance(
                thing_id=thing_id,
                affordance_type=affordance_type,
                affordance_name=name,
                handler_code="def handle(input, state, context):\n    return 1",
                td_definition=td_definition,
            )

        with ThreadPoolExecutor(max_workers=len(specs)) as pool:
            results = list(pool.map(add, specs))
        for result in results:
            self.assertTrue(result["ok"], result)

        final = store.get_definition(thing_id, include_disabled=True)
        self.assertEqual(set(final.td.get("properties", {})), {"p0", "p1", "p2"})
        self.assertEqual(set(final.td.get("actions", {})), {"a0", "a1", "a2"})
        self.assertEqual(len(final.bindings), len(specs))

    def test_add_property_builds_td_and_binding(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Comfort Score")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="currentScore",
            handler_code="def handle(input, state, context):\n    return 72",
            td_definition=property_definition({"type": "number", "readOnly": True}),
        )
        self.assertTrue(result["ok"], result)
        td = result["virtual_thing"]["td"]
        self.assertEqual(td["properties"]["currentScore"]["type"], "number")
        binding = result["virtual_thing"]["bindings"][0]
        self.assertEqual(binding["affordance_type"], "property")
        self.assertEqual(binding["kind"], "computed")

    def test_add_action_records_output_schema(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Hello World")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="action",
            affordance_name="sayHello",
            handler_code="def handle(input, state, context):\n    return 'hello world'",
            td_definition=action_definition(None, {"type": "string"}),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["virtual_thing"]["td"]["actions"]["sayHello"]["output"],
            {"type": "string"},
        )

    def test_add_event_uses_interval_trigger(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Counter")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="event",
            affordance_name="tick",
            handler_code=(
                "def handle(input, state, context):\n"
                "    state = state or {}\n"
                "    counter = state.get('counter', 0) + 1\n"
                "    return {'emit': True, 'payload': {'counter': counter}, "
                "'state': {'counter': counter}}"
            ),
            td_definition=event_definition({"type": "object"}),
            trigger=event_trigger(10, None, None),
        )
        self.assertTrue(result["ok"], result)
        binding = result["virtual_thing"]["bindings"][0]
        self.assertEqual(binding["kind"], "emitted")
        self.assertEqual(binding["trigger"]["kind"], "interval")
        self.assertEqual(binding["trigger"]["interval_seconds"], 10)

    def test_event_trigger_helper_covers_source_and_explicit(self):
        self.assertEqual(event_trigger(None, None, None), {"kind": "explicit"})
        self.assertEqual(
            event_trigger(None, "urn:source-door", "opened"),
            {"kind": "source_event", "thing_id": "urn:source-door", "event_name": "opened"},
        )

    def test_re_adding_affordance_replaces_binding(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Comfort")["thing_id"]
        builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="score",
            handler_code="def handle(input, state, context):\n    return 1",
            td_definition=property_definition({"type": "number"}),
        )
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="score",
            handler_code="def handle(input, state, context):\n    return 2",
            td_definition=property_definition({"type": "number"}),
        )
        bindings = result["virtual_thing"]["bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertIn("return 2", bindings[0]["handler_code"])

    def test_add_affordance_accumulates_distinct_properties_actions_and_events(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Many Affordances")["thing_id"]

        additions = [
            (
                "property",
                "temperature",
                "def handle(input, state, context):\n    return 21",
                property_definition({"type": "number"}),
                None,
            ),
            (
                "property",
                "humidity",
                "def handle(input, state, context):\n    return 42",
                property_definition({"type": "number"}),
                None,
            ),
            (
                "action",
                "refresh",
                "def handle(input, state, context):\n    return {'ok': True}",
                action_definition(None, {"type": "object"}),
                None,
            ),
            (
                "action",
                "reset",
                "def handle(input, state, context):\n    return {'reset': True}",
                action_definition({"type": "object"}, {"type": "object"}),
                None,
            ),
            (
                "event",
                "tick",
                (
                    "def handle(input, state, context):\n"
                    "    return {'emit': True, 'payload': {'tick': True}, 'state': state}"
                ),
                event_definition({"type": "object"}),
                event_trigger(10, None, None),
            ),
            (
                "event",
                "alarm",
                (
                    "def handle(input, state, context):\n"
                    "    return {'emit': True, 'payload': {'alarm': True}, 'state': state}"
                ),
                event_definition({"type": "object"}),
                event_trigger(None, None, None),
            ),
        ]

        result = None
        for affordance_type, name, handler_code, td_definition, trigger in additions:
            result = builder.add_affordance(
                thing_id=thing_id,
                affordance_type=affordance_type,
                affordance_name=name,
                handler_code=handler_code,
                td_definition=td_definition,
                trigger=trigger,
            )
            self.assertTrue(result["ok"], result)

        assert result is not None
        td = result["virtual_thing"]["td"]
        self.assertEqual(set(td["properties"]), {"temperature", "humidity"})
        self.assertEqual(set(td["actions"]), {"refresh", "reset"})
        self.assertEqual(set(td["events"]), {"tick", "alarm"})
        self.assertEqual(
            {
                (binding["affordance_type"], binding["affordance_name"])
                for binding in result["virtual_thing"]["bindings"]
            },
            {
                ("property", "temperature"),
                ("property", "humidity"),
                ("action", "refresh"),
                ("action", "reset"),
                ("event", "tick"),
                ("event", "alarm"),
            },
        )

    def test_add_affordance_rejects_javascript_handler(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Broken JS")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="action",
            affordance_name="sayHello",
            handler_code="function handle(input, state, context) { return 'hello world'; }",
            td_definition=action_definition(None, {"type": "string"}),
        )
        self.assertIn("must be Python", result["error"])

    def test_add_affordance_rejects_bad_handler_signature(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Bad Sig")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="value",
            handler_code="def handle(input):\n    return 1",
            td_definition=property_definition({"type": "number"}),
        )
        self.assertIn("validation failed", result["error"])
        self.assertFalse(result["validation_report"]["ok"])

    def test_add_affordance_infers_capabilities_from_wot_calls(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Grid Headroom")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="headroom",
            handler_code=(
                "def handle(input, state, context):\n"
                "    f = wot.read_property('urn:dev:forecast', 'nextHourKw')\n"
                "    u = wot.read_property('urn:dev:meter', 'powerKw')\n"
                "    return f - u"
            ),
            td_definition=property_definition({"type": "number"}),
        )
        self.assertTrue(result["ok"], result)
        capabilities = result["virtual_thing"]["bindings"][0]["capabilities"]
        thing_ids = {cap["thing_id"] for cap in capabilities}
        self.assertEqual(thing_ids, {"urn:dev:forecast", "urn:dev:meter"})

    def test_add_affordance_infers_capabilities_from_literal_loop(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Average Temp")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="averageTemperature",
            handler_code=(
                "def handle(input, state, context):\n"
                "    SENSORS = [('urn:a', 'temperature'), ('urn:b', 'temperature')]\n"
                "    temps = [wot.read_property(tid, prop) for tid, prop in SENSORS]\n"
                "    return sum(temps) / len(temps)"
            ),
            td_definition=property_definition({"type": "number"}),
        )
        self.assertTrue(result["ok"], result)
        capabilities = result["virtual_thing"]["bindings"][0]["capabilities"]
        self.assertEqual({cap["thing_id"] for cap in capabilities}, {"urn:a", "urn:b"})

    def test_add_affordance_rejects_unscopable_wot_call(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Dynamic Target")["thing_id"]
        result = builder.add_affordance(
            thing_id=thing_id,
            affordance_type="property",
            affordance_name="value",
            handler_code=(
                "def handle(input, state, context):\n"
                "    target = context['config']['thing_id']\n"
                "    return wot.read_property(target, 'value')"
            ),
            td_definition=property_definition({"type": "number"}),
        )
        self.assertIn("error", result)
        messages = [issue["message"] for issue in result["validation_report"]["issues"]]
        self.assertTrue(any("non-literal thing_id" in message for message in messages), messages)

    def test_add_affordance_allows_dynamic_call_with_explicit_capability(self):
        builder, _ = self._builder()
        thing_id = builder.create(title="Declared Dynamic")["thing_id"]
        binding = VirtualThingBindingSpec(
            affordance_type="property",
            affordance_name="value",
            kind="computed",
            handler_code=(
                "def handle(input, state, context):\n"
                "    target = context['config']['thing_id']\n"
                "    return wot.read_property(target, 'value')"
            ),
            capabilities=[
                {"thing_id": "urn:dev:meter", "ops": ["readProperty"], "affordances": ["value"]}
            ],
        )
        request = DefineVirtualThingRequest(
            id=thing_id,
            title="Declared Dynamic",
            td={"title": "Declared Dynamic", "properties": {"value": {"type": "number"}}},
            bindings=[binding],
            status="disabled",
        )
        report = VirtualThingValidator().validate_static(request)
        self.assertTrue(report["ok"], report)

    def test_add_affordance_on_missing_thing_errors(self):
        builder, _ = self._builder()
        result = builder.add_affordance(
            thing_id="urn:wotbot:virtual-things:missing",
            affordance_type="property",
            affordance_name="value",
            handler_code="def handle(input, state, context):\n    return 1",
            td_definition=property_definition(None),
        )
        self.assertEqual(result["error"], "virtual thing not found")


class VirtualThingSchemasTestCase(unittest.TestCase):
    def test_define_request_injects_abstract_forms_and_validates_bindings(self):
        request = DefineVirtualThingRequest(
            title="Comfort Sensor",
            td=_base_td(),
            bindings=[
                {
                    "affordance_type": "property",
                    "affordance_name": "temperature",
                    "kind": "computed",
                    "handler_code": "def handle(input, state, context):\n    return 21",
                }
            ],
        )

        form = request.td["properties"]["temperature"]["forms"][0]
        self.assertEqual(form["op"], ["readproperty"])
        self.assertTrue(form["href"].startswith("urn:wotbot:virtual-things:"))

    def test_define_request_injects_default_security_boilerplate(self):
        request = DefineVirtualThingRequest(
            title="Comfort Sensor",
            td={"properties": {"temperature": {"type": "number"}}},
            bindings=[
                {
                    "affordance_type": "property",
                    "affordance_name": "temperature",
                    "kind": "computed",
                    "handler_code": "def handle(input, state, context):\n    return 21",
                }
            ],
        )

        self.assertEqual(request.td["@context"], "https://www.w3.org/2022/wot/td/v1.1")
        self.assertEqual(request.td["security"], "nosec_sc")
        self.assertEqual(request.td["securityDefinitions"]["nosec_sc"]["scheme"], "nosec")

    def test_define_request_rejects_missing_binding_affordance(self):
        with self.assertRaisesRegex(Exception, "missing property"):
            DefineVirtualThingRequest(
                title="Comfort Sensor",
                td=_base_td(),
                bindings=[
                    {
                        "affordance_type": "property",
                        "affordance_name": "missing",
                        "kind": "computed",
                        "handler_code": "def handle(input, state, context):\n    return 21",
                    }
                ],
            )

    def test_define_request_accepts_handle_alias_for_handler_code(self):
        request = DefineVirtualThingRequest(
            title="Comfort Sensor",
            td=_base_td(),
            bindings=[
                {
                    "affordance_type": "property",
                    "affordance_name": "temperature",
                    "kind": "computed",
                    "handle": "def handle(input, state, context):\n    return 21",
                }
            ],
        )

        self.assertIn("return 21", request.bindings[0].handler_code or "")

    def test_define_request_rejects_javascript_handler_with_clear_error(self):
        with self.assertRaisesRegex(Exception, "must be Python code"):
            DefineVirtualThingRequest(
                title="Comfort Sensor",
                td=_base_td(),
                bindings=[
                    {
                        "affordance_type": "property",
                        "affordance_name": "temperature",
                        "kind": "computed",
                        "handle": "function handle(input, state, context) { return 21; }",
                    }
                ],
            )

    def test_define_request_accepts_event_binding_shorthand(self):
        request = DefineVirtualThingRequest(
            title="Counter Event Thing",
            td={
                "events": {
                    "tick": {
                        "data": {
                            "type": "object",
                            "properties": {"counter": {"type": "integer"}},
                        },
                        "evaluationInterval": 10000,
                    }
                }
            },
            bindings=[
                {
                    "affordance": "tick",
                    "kind": "event",
                    "source": (
                        "def handle(input, state, context):\n"
                        "    state = state or {}\n"
                        "    counter = int(state.get('counter', 0)) + 1\n"
                        "    return {'emit': True, 'payload': {'counter': counter}, "
                        "'state': {'counter': counter}}"
                    ),
                    "state": {"counter": 0},
                }
            ],
        )

        binding = request.bindings[0]
        self.assertEqual(binding.affordance_type, "event")
        self.assertEqual(binding.affordance_name, "tick")
        self.assertEqual(binding.kind, "emitted")
        self.assertEqual(binding.trigger.interval_seconds if binding.trigger else None, 10)


class VirtualThingCapabilityInferenceTestCase(unittest.TestCase):
    def test_infers_grants_for_each_source_thing(self):
        handler = (
            "def handle(input, state, context):\n"
            "    forecast = wot.read_property('urn:dev:forecast', 'nextHourKw')\n"
            "    usage = wot.read_property('urn:dev:meter', 'powerKw')\n"
            "    wot.invoke_action('urn:dev:meter', 'reset')\n"
            "    return forecast - usage"
        )

        capabilities = infer_capabilities(handler)

        self.assertEqual(
            capabilities,
            [
                {
                    "thing_id": "urn:dev:forecast",
                    "ops": ["readProperty"],
                    "affordances": ["nextHourKw"],
                },
                {
                    "thing_id": "urn:dev:meter",
                    "ops": ["invokeAction", "readProperty"],
                    "affordances": ["powerKw", "reset"],
                },
            ],
        )

    def test_dynamic_thing_id_is_not_inferred(self):
        handler = (
            "def handle(input, state, context):\n"
            "    target = context['config']['thing_id']\n"
            "    return wot.read_property(target, 'value')"
        )

        self.assertEqual(infer_capabilities(handler), [])

    def test_dynamic_affordance_grants_all_affordances(self):
        handler = (
            "def handle(input, state, context):\n"
            "    return wot.read_property('urn:dev:meter', input['name'])"
        )

        self.assertEqual(
            infer_capabilities(handler),
            [{"thing_id": "urn:dev:meter", "ops": ["readProperty"], "affordances": []}],
        )

    def test_binding_merges_explicit_and_inferred_capabilities(self):
        binding = VirtualThingBindingSpec(
            affordance_type="property",
            affordance_name="headroom",
            kind="computed",
            handler_code=(
                "def handle(input, state, context):\n"
                "    return wot.read_property('urn:dev:meter', 'powerKw')"
            ),
            capabilities=[
                {
                    "thing_id": "urn:dev:forecast",
                    "ops": ["readProperty"],
                    "affordances": ["nextHourKw"],
                }
            ],
        )

        grants = {cap.thing_id: cap for cap in binding.capabilities}
        self.assertEqual(set(grants), {"urn:dev:forecast", "urn:dev:meter"})
        self.assertEqual(grants["urn:dev:meter"].ops, ["readProperty"])
        self.assertEqual(grants["urn:dev:meter"].affordances, ["powerKw"])

    def test_infers_grants_from_literal_source_loop(self):
        handler = (
            "def handle(input, state, context):\n"
            "    SOURCES = [\n"
            "        ('urn:factory:line-a', 'unitsProduced'),\n"
            "        ('urn:factory:line-b', 'state'),\n"
            "    ]\n"
            "    readings = []\n"
            "    for tid, prop in SOURCES:\n"
            "        readings.append(wot.read_property(tid, prop))\n"
            "    return readings"
        )

        self.assertEqual(
            infer_capabilities(handler),
            [
                {
                    "thing_id": "urn:factory:line-a",
                    "ops": ["readProperty"],
                    "affordances": ["unitsProduced"],
                },
                {
                    "thing_id": "urn:factory:line-b",
                    "ops": ["readProperty"],
                    "affordances": ["state"],
                },
            ],
        )
        self.assertEqual(find_unscopable_wot_calls(handler), [])

    def test_loop_over_literal_ids_with_literal_property(self):
        handler = (
            "def handle(input, state, context):\n"
            "    IDS = ['urn:a', 'urn:b']\n"
            "    for tid in IDS:\n"
            "        wot.read_property(tid, 'temperature')\n"
            "    return None"
        )

        self.assertEqual(
            infer_capabilities(handler),
            [
                {"thing_id": "urn:a", "ops": ["readProperty"], "affordances": ["temperature"]},
                {"thing_id": "urn:b", "ops": ["readProperty"], "affordances": ["temperature"]},
            ],
        )

    def test_find_unscopable_flags_dynamic_thing_id(self):
        handler = (
            "def handle(input, state, context):\n"
            "    target = context['config']['thing_id']\n"
            "    return wot.read_property(target, 'value')"
        )

        self.assertEqual(infer_capabilities(handler), [])
        self.assertEqual(find_unscopable_wot_calls(handler), ["read_property"])

    def test_find_unscopable_clean_for_literal_calls(self):
        handler = (
            "def handle(input, state, context):\n"
            "    return wot.read_property('urn:dev:meter', 'powerKw')"
        )

        self.assertEqual(find_unscopable_wot_calls(handler), [])


class VirtualThingDispatcherTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_computed_property_cache_lives_in_dispatcher(self):
        binding = SimpleNamespace(
            id="binding-1",
            thing_id="virtual:things:comfort",
            affordance_type="property",
            affordance_name="temperature",
            kind="computed",
            handler_code="def handle(input, state, context):\n    return 21",
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=7,
            cache_ttl_seconds=30,
        )
        executor = _FakeExecutor(["21", "22"])
        dispatcher = VirtualThingDispatcher(
            store=_FakeStore(binding),
            record_store=SimpleNamespace(),
            code_executor=executor,
        )

        self.assertEqual(
            await dispatcher.read_property("virtual:things:comfort", "temperature"), 21
        )
        self.assertEqual(
            await dispatcher.read_property("virtual:things:comfort", "temperature"), 21
        )
        self.assertEqual(executor.calls, 1)
        self.assertEqual(executor.kwargs[0]["timeout_seconds"], 7)

    async def test_action_shared_state_is_visible_to_cached_property(self):
        property_binding = SimpleNamespace(
            id="property-binding",
            thing_id="virtual:things:controller",
            affordance_type="property",
            affordance_name="temperature",
            kind="computed",
            handler_code=(
                "def handle(input, state, context):\n"
                "    return context['shared_state'].get('mode', 'unset')"
            ),
            capabilities=[],
            config={},
            state={"local": "property"},
            timeout_seconds=30,
            cache_ttl_seconds=30,
        )
        action_binding = SimpleNamespace(
            id="action-binding",
            thing_id="virtual:things:controller",
            affordance_type="action",
            affordance_name="refresh",
            kind="computed",
            handler_code=(
                "def handle(input, state, context):\n"
                "    context['shared_state']['mode'] = input['mode']\n"
                "    return {'ok': True}"
            ),
            capabilities=[],
            config={},
            state={"local": "action"},
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        store = _SharedStateStore(
            {
                ("property", "temperature"): property_binding,
                ("action", "refresh"): action_binding,
            },
            shared_state={"mode": "away"},
        )
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        self.assertEqual(
            await dispatcher.read_property("virtual:things:controller", "temperature"),
            "away",
        )
        self.assertEqual(
            await dispatcher.invoke_action(
                "virtual:things:controller",
                "refresh",
                {"mode": "eco"},
            ),
            {"ok": True},
        )
        self.assertEqual(store.shared_state, {"mode": "eco"})
        self.assertEqual(
            await dispatcher.read_property("virtual:things:controller", "temperature"),
            "eco",
        )

    async def test_action_shared_state_conflict_is_not_retried(self):
        binding = SimpleNamespace(
            id="action-binding",
            thing_id="virtual:things:controller",
            affordance_type="action",
            affordance_name="refresh",
            kind="computed",
            handler_code=(
                "def handle(input, state, context):\n"
                "    context['shared_state']['mode'] = 'eco'\n"
                "    return {'ok': True}"
            ),
            capabilities=[],
            config={},
            state={},
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        store = _SharedStateStore({("action", "refresh"): binding})
        store.reject_shared_updates = True
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        with self.assertRaises(VirtualThingStateConflict):
            await dispatcher.invoke_action("virtual:things:controller", "refresh", {})

    async def test_event_evaluate_persists_state_and_suppresses_null_emit(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:comfort",
            affordance_type="event",
            affordance_name="threshold_crossed",
            kind="emitted",
            handler_code="def handle(input, state, context):\n    return {}",
            capabilities=[],
            config={},
            state={"was_below": False},
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        store = _FakeStore(binding)
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_FakeExecutor(
                ['{"emit": false, "payload": {"ignored": true}, "state": {"was_below": true}}']
            ),
        )

        self.assertIsNone(
            await dispatcher.evaluate_event(
                "virtual:things:comfort",
                "threshold_crossed",
                {"value": 19},
            )
        )
        self.assertEqual(store.updated_state, ("event-binding", {"was_below": True}))

    async def test_event_evaluate_defaults_missing_state_to_empty_object(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:counter",
            affordance_type="event",
            affordance_name="tick",
            kind="emitted",
            handler_code=(
                "def handle(input, state, context):\n"
                "    counter = state.get('counter', 0) + 1\n"
                "    return {'emit': True, 'payload': {'counter': counter}, "
                "'state': {'counter': counter}}"
            ),
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        store = _FakeStore(binding)
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        self.assertEqual(
            await dispatcher.evaluate_event("virtual:things:counter", "tick", {}),
            {"counter": 1},
        )
        self.assertEqual(store.updated_state, ("event-binding", {"counter": 1}))

    async def test_event_evaluate_persists_shared_state_with_binding_state(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:counter",
            affordance_type="event",
            affordance_name="tick",
            kind="emitted",
            handler_code=(
                "def handle(input, state, context):\n"
                "    count = context['shared_state'].get('count', 0) + 1\n"
                "    context['shared_state']['count'] = count\n"
                "    return {'emit': True, 'payload': {'count': count}, "
                "'state': {'seen': count}}"
            ),
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=30,
            cache_ttl_seconds=0,
            shared_state={"count": 0},
            shared_state_version=1,
        )
        store = _FakeStore(binding)
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        self.assertEqual(
            await dispatcher.evaluate_event("virtual:things:counter", "tick", {}),
            {"count": 1},
        )
        self.assertEqual(store.updated_state, ("event-binding", {"seen": 1}))
        self.assertEqual(
            store.updated_shared_state,
            ("virtual:things:counter", 1, {"count": 1}),
        )

    async def test_event_evaluate_dry_run_does_not_persist_state(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:counter",
            affordance_type="event",
            affordance_name="tick",
            kind="emitted",
            handler_code=(
                "def handle(input, state, context):\n"
                "    context['shared_state']['counter'] = 1\n"
                "    return {'emit': True, 'payload': {'counter': 1}, "
                "'state': {'counter': 1}}"
            ),
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=30,
            cache_ttl_seconds=0,
            shared_state={},
            shared_state_version=1,
        )
        store = _FakeStore(binding)
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        self.assertEqual(
            await dispatcher.evaluate_event("virtual:things:counter", "tick", {}, dry_run=True),
            {"counter": 1},
        )
        self.assertIsNone(store.updated_state)
        self.assertIsNone(store.updated_shared_state)

    async def test_event_evaluate_rejects_none_result_with_hint(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:counter",
            affordance_type="event",
            affordance_name="tick",
            kind="emitted",
            handler_code="def handle(input, state, context):\n    return None",
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        dispatcher = VirtualThingDispatcher(
            store=_FakeStore(binding),
            record_store=SimpleNamespace(),
            code_executor=_FakeExecutor(["null"]),
        )

        with self.assertRaisesRegex(ValueError, "returned None"):
            await dispatcher.evaluate_event("virtual:things:counter", "tick", {})

    async def test_emit_event_enqueues_emission_when_handler_emits(self):
        binding = SimpleNamespace(
            id="event-binding",
            thing_id="virtual:things:manual",
            affordance_type="event",
            affordance_name="signal",
            kind="emitted",
            handler_code=(
                "def handle(input, state, context):\n"
                "    return {'emit': True, 'payload': input['input'], 'state': state}"
            ),
            capabilities=[],
            config={},
            state=None,
            timeout_seconds=30,
            cache_ttl_seconds=0,
        )
        store = _FakeStore(binding)
        dispatcher = VirtualThingDispatcher(
            store=store,
            record_store=SimpleNamespace(),
            code_executor=_LocalExecutor(),
        )

        result = await dispatcher.emit_event(
            "virtual:things:manual",
            "signal",
            {"trigger": "explicit", "input": {"ok": True}},
        )

        self.assertEqual(
            result,
            {
                "thing_id": "virtual:things:manual",
                "event_name": "signal",
                "emitted": True,
                "payload": {"ok": True},
            },
        )
        self.assertEqual(
            store.enqueued_emission,
            ("virtual:things:manual", "signal", {"ok": True}),
        )


class VirtualThingValidatorTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_validation_defaults_missing_event_state(self):
        request = DefineVirtualThingRequest(
            title="Counter Tick Event",
            td={
                "events": {
                    "tick": {
                        "data": {
                            "type": "object",
                            "properties": {"counter": {"type": "integer"}},
                            "required": ["counter"],
                        }
                    }
                }
            },
            bindings=[
                {
                    "affordance_type": "event",
                    "affordance_name": "tick",
                    "kind": "emitted",
                    "trigger": {"kind": "interval", "interval_seconds": 10},
                    "handler_code": (
                        "def handle(input, state, context):\n"
                        "    counter = state.get('counter', 0) + 1\n"
                        "    return {'emit': True, 'payload': {'counter': counter}, "
                        "'state': {'counter': counter}}"
                    ),
                }
            ],
        )

        report = await VirtualThingValidator(code_executor=_LocalExecutor()).validate(
            request,
            run_smoke=True,
        )

        self.assertTrue(report["ok"], report)
        self.assertTrue(report["smoke_tested"])

    async def test_smoke_validation_rejects_event_that_only_works_with_seed_state(self):
        request = DefineVirtualThingRequest(
            title="Counter Tick Event",
            td={
                "events": {
                    "tick": {
                        "data": {
                            "type": "object",
                            "properties": {"counter": {"type": "integer"}},
                            "required": ["counter"],
                        }
                    }
                }
            },
            bindings=[
                {
                    "affordance_type": "event",
                    "affordance_name": "tick",
                    "kind": "emitted",
                    "trigger": {"kind": "interval", "interval_seconds": 10},
                    "state": {"counter": 0},
                    "handler_code": (
                        "def handle(input, state, context):\n"
                        "    if state.get('counter') is not None:\n"
                        "        counter = state['counter'] + 1\n"
                        "        return {'emit': True, 'payload': {'counter': counter}, "
                        "'state': {'counter': counter}}"
                    ),
                }
            ],
        )

        report = await VirtualThingValidator(code_executor=_LocalExecutor()).validate(
            request,
            run_smoke=True,
        )

        self.assertFalse(report["ok"])
        self.assertIn("empty state", report["issues"][0]["message"])
        self.assertIn("returned None", report["issues"][0]["message"])

    async def test_smoke_validation_rejects_event_that_fails_on_next_state(self):
        request = DefineVirtualThingRequest(
            title="Counter Tick Event",
            td={
                "events": {
                    "tick": {
                        "data": {
                            "type": "object",
                            "properties": {"counter": {"type": "integer"}},
                            "required": ["counter"],
                        }
                    }
                }
            },
            bindings=[
                {
                    "affordance_type": "event",
                    "affordance_name": "tick",
                    "kind": "emitted",
                    "trigger": {"kind": "interval", "interval_seconds": 10},
                    "handler_code": (
                        "def handle(input, state, context):\n"
                        "    counter = state.get('counter', 0)\n"
                        "    if counter == 0:\n"
                        "        return {'emit': True, 'payload': {'counter': 1}, "
                        "'state': {'counter': 1}}\n"
                        "    return None"
                    ),
                }
            ],
        )

        report = await VirtualThingValidator(code_executor=_LocalExecutor()).validate(
            request,
            run_smoke=True,
        )

        self.assertFalse(report["ok"])
        self.assertIn("next state", report["issues"][0]["message"])
        self.assertIn("returned None", report["issues"][0]["message"])

    async def test_smoke_validation_rejects_event_payload_schema_mismatch(self):
        request = DefineVirtualThingRequest(
            title="Counter Tick Event",
            td={
                "events": {
                    "tick": {
                        "data": {
                            "type": "object",
                            "properties": {"counter": {"type": "integer"}},
                            "required": ["counter"],
                        }
                    }
                }
            },
            bindings=[
                {
                    "affordance_type": "event",
                    "affordance_name": "tick",
                    "kind": "emitted",
                    "trigger": {"kind": "interval", "interval_seconds": 10},
                    "handler_code": (
                        "def handle(input, state, context):\n"
                        "    return {'emit': True, 'payload': {'counter': 'bad'}, "
                        "'state': {}}"
                    ),
                }
            ],
        )

        report = await VirtualThingValidator(code_executor=_LocalExecutor()).validate(
            request,
            run_smoke=True,
        )

        self.assertFalse(report["ok"])
        self.assertIn("schema validation", report["issues"][0]["message"])

    async def test_activate_smoke_uses_seeded_shared_state_for_direct_indexing(self):
        thing_id = "urn:wotbot:virtual-things:tv"
        store = _FakeDefinitionStore()
        store.things[thing_id] = VirtualThingDefinition(
            id=thing_id,
            title="TV",
            description="",
            owner_thread_id=None,
            td={
                "@context": "https://www.w3.org/2022/wot/td/v1.1",
                "id": thing_id,
                "title": "TV",
                "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
                "security": "nosec_sc",
                "properties": {"power": {"type": "boolean"}},
            },
            version=1,
            status="disabled",
            shared_state={"power": False},
            bindings=[
                VirtualThingBindingSpec(
                    affordance_type="property",
                    affordance_name="power",
                    kind="computed",
                    handler_code=(
                        "def handle(input, state, context):\n"
                        "    return context['shared_state']['power']"
                    ),
                )
            ],
        )
        builder = VirtualThingBuilder(
            store=store,
            validator=VirtualThingValidator(code_executor=_LocalExecutor()),
        )

        with patch(
            "wotbot.virtual_things.builder.get_enrichment_scheduler",
            return_value=SimpleNamespace(schedule=lambda *_args, **_kwargs: None),
        ):
            result = await builder.activate(thing_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["virtual_thing"]["shared_state"], {"power": False})

    async def test_activate_does_not_persist_when_smoke_validation_fails(self):
        class RejectingValidator:
            async def validate(self, _request, *, run_smoke):
                self.run_smoke = run_smoke
                return {
                    "ok": False,
                    "smoke_tested": True,
                    "issues": [{"phase": "smoke", "message": "boom"}],
                }

        thing_id = "urn:wotbot:virtual-things:broken"
        store = _FakeDefinitionStore()
        store.things[thing_id] = VirtualThingDefinition(
            id=thing_id,
            title="Broken",
            description="",
            owner_thread_id=None,
            td={
                "@context": "https://www.w3.org/2022/wot/td/v1.1",
                "id": thing_id,
                "title": "Broken",
                "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
                "security": "nosec_sc",
                "properties": {"value": {"type": "number"}},
            },
            version=1,
            status="disabled",
            bindings=[
                VirtualThingBindingSpec(
                    affordance_type="property",
                    affordance_name="value",
                    kind="computed",
                    handler_code="def handle(input, state, context):\n    return 1",
                )
            ],
        )
        validator = RejectingValidator()
        builder = VirtualThingBuilder(store=store, validator=validator)

        result = await builder.activate(thing_id)

        self.assertEqual(result["error"], "virtual thing validation failed")
        self.assertFalse(result["validation_report"]["ok"])
        self.assertTrue(validator.run_smoke)
        self.assertEqual(store.requests, [])


if __name__ == "__main__":
    unittest.main()
