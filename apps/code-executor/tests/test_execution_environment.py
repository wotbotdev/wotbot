import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from code_executor.constants import MAX_STDOUT_CHARS
from code_executor.execution_environment import ExecutionEnvironment
from code_executor.file_artifacts import FileArtifacts
from code_executor.models import Settings
from code_executor.models.schemas import ExecuteResponse
from code_executor.worker import (
    _receive_child_result,
    _run_child_code,
    _session_response,
)

RESULT_PREFIX = "__VIRTUAL_THING_RESULT__"


class ExecutionEnvironmentOutputTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.env = ExecutionEnvironment.__new__(ExecutionEnvironment)
        self.env.artifacts_dir = self._tmp
        self.env.file_store = FileArtifacts(
            Settings(_env_file=None, artifacts_dir=self._tmp)
        )
        self.env.files = []
        self.env.images = []
        self.env.plotly = []
        self.env.wot_calls = []
        self.env.records = []
        self.env.reports = []
        self.env.plt = SimpleNamespace(show=lambda: None)
        self.env.pio = SimpleNamespace(
            show=lambda: None,
            renderers=SimpleNamespace(default=""),
        )
        self.env.user_globals = {
            "__builtins__": __builtins__,
            "print": print,
            "report": self.env.report,
            "store_record": self.env.store_record,
        }

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_echoes_final_expression_when_no_visible_output(self) -> None:
        result = self.env.execute_code("result = {'ok': True}\nresult")

        self.assertEqual(result["stdout"], "{'ok': True}\n")

    def test_store_record_accepts_text_and_json_raw_input(self) -> None:
        for raw_input in (
            None,
            "125 cm",
            {"measurement": {"value": "125", "unit": "cm"}},
            [{"value": "0"}, None],
            0,
            1.25,
            False,
        ):
            with self.subTest(raw_input=raw_input):
                result = self.env.execute_code(
                    f"store_record({{'metres': 1.25}}, raw_input={raw_input!r}, confidence=0.9)"
                )
                wire = ExecuteResponse(**result).model_dump()
                self.assertTrue(wire["ok"], wire["error"])
                record = wire["records"][0]
                self.assertEqual(record["data"], {"metres": 1.25})
                self.assertEqual(record["confidence"], 0.9)
                if raw_input is None or isinstance(raw_input, str):
                    self.assertEqual(record["raw_input"], raw_input)
                else:
                    self.assertEqual(json.loads(record["raw_input"]), raw_input)

    def test_invalid_records_fail_inside_execution_and_discard_outputs(self) -> None:
        for arguments in (
            "{'metres': 1.25}, raw_input=object()",
            "{'metres': 1.25}, raw_input={'value': float('nan')}",
            "{'metres': 1.25}, confidence='high'",
            "{'metres': 1.25}, confidence=float('inf')",
            "{'metres': object()}",
            "{'metres': float('nan')}",
        ):
            with self.subTest(arguments=arguments):
                result = self.env.execute_code(
                    "store_record({'metres': 0})\n"
                    "report('partial')\n"
                    f"store_record({arguments})"
                )
                wire = ExecuteResponse(**result).model_dump()
                self.assertFalse(wire["ok"])
                self.assertTrue(wire["error"])
                self.assertEqual(wire["records"], [])
                self.assertEqual(wire["reports"], [])

    def test_store_record_snapshots_data_before_later_mutation(self) -> None:
        result = self.env.execute_code(
            "data = {'measurement': {'value': 125}}\n"
            "store_record(data, raw_input=data)\n"
            "data['measurement']['value'] = object()"
        )
        wire = ExecuteResponse(**result).model_dump()
        self.assertTrue(wire["ok"], wire["error"])
        self.assertEqual(wire["records"][0]["data"], {"measurement": {"value": 125}})
        self.assertEqual(
            json.loads(wire["records"][0]["raw_input"]), wire["records"][0]["data"]
        )

    def test_does_not_duplicate_final_expression_when_stdout_exists(self) -> None:
        result = self.env.execute_code("result = {'ok': True}\nprint(result)\nresult")

        self.assertEqual(result["stdout"], "{'ok': True}\n")

    def test_failure_preserves_status_and_completed_calls_through_worker_and_api(
        self,
    ) -> None:
        completed = {
            "type": "write_property",
            "thing_id": "urn:test:device",
            "name": "target",
            "ok": True,
            "input": 24,
        }
        self.env.user_globals["write"] = lambda: self.env.wot_calls.append(completed)
        result = self.env.execute_code(
            f"write(); print('x' * {MAX_STDOUT_CHARS + 1}); raise ValueError('after write')"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "ValueError: after write")
        self.assertNotIn(
            "ValueError", result["stdout"], "Exercise a truncated error line"
        )
        with patch("code_executor.worker.os.waitpid", return_value=(123, 0)):
            failed, promoted = _receive_child_result(
                SimpleNamespace(recv=lambda: result), 123
            )
        self.assertFalse(promoted)
        wire = ExecuteResponse(**failed).model_dump()
        self.assertFalse(wire["ok"])
        self.assertEqual(wire["error"], "ValueError: after write")
        self.assertEqual(wire["wot_calls"][0]["thing_id"], "urn:test:device")
        for key in ("images", "plotly", "records", "reports"):
            self.assertEqual(wire[key], [])

    def test_successful_output_containing_error_text_stays_successful(self) -> None:
        result = self.env.execute_code("print('Error: an example, not an exception')")
        wire = ExecuteResponse(**_session_response(result))
        self.assertTrue(wire.ok)
        self.assertIsNone(wire.error)

    def test_system_exit_preserves_completed_calls_and_discards_outputs(self) -> None:
        completed = {
            "type": "invoke_action",
            "thing_id": "urn:test:device",
            "name": "toggle",
            "ok": True,
        }
        self.env.user_globals["write"] = lambda: self.env.wot_calls.append(completed)
        self.env.user_globals["save_artifact"] = self.env.save_artifact
        self.env.user_globals["save_image"] = self.env.save_image
        sent = []
        _run_child_code(
            self.env,
            "write(); report('done'); print('before exit'); "
            "save_artifact('partial', filename='partial.txt'); save_image(b'partial'); "
            "raise SystemExit('after write')",
            SimpleNamespace(send=sent.append),
        )
        wire = ExecuteResponse(**_session_response(sent[0])).model_dump()
        self.assertFalse(wire["ok"])
        self.assertEqual(wire["error"], "SystemExit: after write")
        self.assertEqual(wire["wot_calls"][0]["name"], "toggle")
        self.assertEqual(wire["reports"], [])
        self.assertIn("before exit", wire["stdout"])
        self.assertEqual(list(Path(self._tmp).glob("*.png")), [])
        self.assertEqual(list((Path(self._tmp) / ".staging").iterdir()), [])

    def test_does_not_echo_final_expression_when_report_exists(self) -> None:
        result = self.env.execute_code("report('done')\n{'debug': True}")

        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["reports"], ["done"])

    def test_ignores_none_final_expression(self) -> None:
        result = self.env.execute_code("None")

        self.assertEqual(result["stdout"], "")

    def test_trims_large_human_stdout(self) -> None:
        result = self.env.execute_code(f"print({'x' * (MAX_STDOUT_CHARS + 50)!r})")

        self.assertIn("... truncated", result["stdout"])
        self.assertLess(len(result["stdout"]), MAX_STDOUT_CHARS + 200)

    def test_preserves_virtual_thing_result_line_when_stdout_is_trimmed(self) -> None:
        payload = (
            RESULT_PREFIX + '{"payload":"' + ("x" * (MAX_STDOUT_CHARS + 50)) + '"}'
        )
        result = self.env.execute_code(
            f"print({'debug ' + ('d' * MAX_STDOUT_CHARS)!r})\nprint({payload!r})"
        )

        self.assertIn("... truncated", result["stdout"])
        self.assertIn(payload, result["stdout"].splitlines())


if __name__ == "__main__":
    unittest.main()
