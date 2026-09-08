import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wotbot.clients.code_executor import CodeExecutionUncertainError
from wotbot.virtual_things.handler import (
    RESULT_PREFIX,
    HandlerContext,
    VirtualThingHandlerError,
    VirtualThingHandlerRunner,
    decode_result_envelope,
    handler_wrapper,
)

_TOKEN = "0123456789abcdef0123456789abcdef"


def _fake_wot(**overrides):
    base = {
        "read_property": lambda *a, **k: 42,
        "write_property": lambda *a, **k: None,
        "invoke_action": lambda *a, **k: {"ok": True},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _result_from_stdout(stdout: str) -> dict:
    found, envelope = decode_result_envelope(stdout, _TOKEN)
    if not found:
        raise AssertionError("handler produced no result line")
    return envelope


def _run(code: str, wot) -> dict:
    namespace = {"wot": wot}
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exec(code, namespace)
    return _result_from_stdout(stdout.getvalue())


def _context(capabilities):
    return HandlerContext(
        thing_id="virtual:thing",
        affordance_type="action",
        affordance_name="disaggregate",
        capabilities=capabilities,
        config={},
        shared_state={},
        shared_state_version=1,
    )


class VirtualThingGuardedWotTestCase(unittest.TestCase):
    def test_declared_capability_call_succeeds(self) -> None:
        """Regression: the guard class referenced ``__vt_check`` which Python
        name-mangled, raising NameError the first time a handler hit a real
        capability call."""
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                "    return wot.invoke_action('urn:nilm', 'disaggregate', input)"
            ),
            input_value={"series": [1, 2, 3]},
            state={},
            context=_context(
                [{"thing_id": "urn:nilm", "ops": ["invokeAction"], "affordances": ["disaggregate"]}]
            ),
            result_token=_TOKEN,
        )

        result = _run(code, _fake_wot(invoke_action=lambda *a, **k: {"fridge": 0.4}))

        self.assertEqual(result["value"], {"fridge": 0.4})

    def test_declared_capability_call_succeeds_after_reused_namespace(self) -> None:
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                "    return wot.invoke_action('urn:nilm', 'disaggregate', input)"
            ),
            input_value={"series": [1, 2, 3]},
            state={},
            context=_context(
                [{"thing_id": "urn:nilm", "ops": ["invokeAction"], "affordances": ["disaggregate"]}]
            ),
            result_token=_TOKEN,
        )
        namespace = {"wot": _fake_wot(invoke_action=lambda *a, **k: {"fridge": 0.4})}

        first_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            exec(code, namespace)
        second_stdout = io.StringIO()
        with contextlib.redirect_stdout(second_stdout):
            exec(code, namespace)

        self.assertEqual(_result_from_stdout(first_stdout.getvalue())["value"], {"fridge": 0.4})
        self.assertEqual(_result_from_stdout(second_stdout.getvalue())["value"], {"fridge": 0.4})

    def test_shared_state_is_exposed_and_returned(self) -> None:
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                "    context['shared_state']['mode'] = input['mode']\n"
                "    return {'ok': True}"
            ),
            input_value={"mode": "eco"},
            state={},
            context=_context([]),
            result_token=_TOKEN,
        )

        result = _run(code, _fake_wot())

        self.assertEqual(result["value"], {"ok": True})
        self.assertEqual(result["shared_state"], {"mode": "eco"})

    def test_undeclared_capability_is_blocked(self) -> None:
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                "    return wot.read_property('urn:secret', 'power')"
            ),
            input_value=None,
            state={},
            context=_context(
                [{"thing_id": "urn:nilm", "ops": ["invokeAction"], "affordances": ["disaggregate"]}]
            ),
            result_token=_TOKEN,
        )

        with self.assertRaises(PermissionError):
            _run(code, _fake_wot())


class VirtualThingExecutionStatusTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.binding = SimpleNamespace(
            id="binding",
            thing_id="virtual:thing",
            affordance_type="property",
            affordance_name="value",
            handler_code="def handle(input, state, context): return 1",
            capabilities=[],
            config={},
            timeout_seconds=30,
        )

    async def test_failed_execution_uses_error_summary_when_stdout_is_truncated(self) -> None:
        executor = AsyncMock()
        executor.execute.return_value = {
            "ok": False,
            "error": "ValueError: missing source data",
            "stdout": "debug output ... truncated",
        }
        with self.assertRaisesRegex(VirtualThingHandlerError, "ValueError: missing source data"):
            await VirtualThingHandlerRunner(executor).run_handler(
                self.binding,
                input_value=None,
                state={},
            )

    async def test_uncertain_execution_preserves_recovery_guidance(self) -> None:
        executor = AsyncMock()
        executor.execute.side_effect = CodeExecutionUncertainError()
        with self.assertRaisesRegex(VirtualThingHandlerError, "may already have applied"):
            await VirtualThingHandlerRunner(executor).run_handler(
                self.binding,
                input_value=None,
                state={},
            )
        executor.execute.assert_awaited_once()


class VirtualThingResultChannelTestCase(unittest.TestCase):
    def test_handler_print_cannot_forge_result(self) -> None:
        """A handler that prints the bare RESULT_PREFIX (and other noise) must not
        be mistaken for the real, token-tagged envelope."""
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                f"    print({RESULT_PREFIX!r} + 'not the real result')\n"
                "    print('plain log line')\n"
                "    return {'real': True}"
            ),
            input_value=None,
            state={},
            context=_context([]),
            result_token=_TOKEN,
        )

        result = _run(code, _fake_wot())

        self.assertEqual(result["value"], {"real": True})

    def test_multiline_stdout_before_result_is_ignored(self) -> None:
        code = handler_wrapper(
            handler_code=(
                "def handle(input, state, context):\n"
                "    print('line one\\nline two\\nline three')\n"
                "    return {'n': 3}"
            ),
            input_value=None,
            state={},
            context=_context([]),
            result_token=_TOKEN,
        )

        result = _run(code, _fake_wot())

        self.assertEqual(result["value"], {"n": 3})

    def test_wrong_token_is_not_decoded(self) -> None:
        code = handler_wrapper(
            handler_code="def handle(input, state, context):\n    return 1",
            input_value=None,
            state={},
            context=_context([]),
            result_token="deadbeef",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exec(code, {"wot": _fake_wot()})

        # The real run used a different token, so this run's marker must not match.
        found, _ = decode_result_envelope(stdout.getvalue(), _TOKEN)
        self.assertFalse(found)


if __name__ == "__main__":
    unittest.main()
