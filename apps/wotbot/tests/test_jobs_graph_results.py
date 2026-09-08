from __future__ import annotations

import unittest
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from wotbot.jobs.graph_results import (
    code_result_from_graph_result,
    failed_record_submission_from_graph_result,
    graph_input_for_run,
    job_result_from_graph_result,
    job_run_status_from_result,
    parse_graph_result,
    submitted_record_from_graph_result,
    waiting_question_from_graph_result,
)
from wotbot.jobs.models import (
    Job,
    JobActionKind,
    JobInteractionMode,
    JobOutputKind,
    JobRun,
    JobRunSource,
    JobRunStatus,
    JobTriggerKind,
    TimeTriggerKind,
)


def _job(**overrides) -> Job:
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    action_kind = overrides.pop("action_kind", JobActionKind.PROMPT)
    prompt = overrides.pop("prompt", "Check the production queue")
    analysis_code = overrides.pop("analysis_code", "print('ok')")
    output_kind = overrides.pop("output_kind", JobOutputKind.NARRATIVE)
    trigger_kind = overrides.pop("trigger_kind", JobTriggerKind.TIME)
    schedule_kind = overrides.pop("schedule_kind", TimeTriggerKind.INTERVAL)
    run_at = overrides.pop("run_at", None)
    interval_seconds = overrides.pop("interval_seconds", 60)
    cron_expression = overrides.pop("cron_expression", None)
    cron_timezone = overrides.pop("cron_timezone", None)
    thing_id = overrides.pop("thing_id", "thing-1")
    event_name = overrides.pop("event_name", "changed")
    subscription_input = overrides.pop("subscription_input", None)
    record_schema = overrides.pop(
        "record_schema",
        {"type": "object", "properties": {"mood": {"type": "string"}}},
    )
    record_schema_version = overrides.pop("record_schema_version", 1)
    virtual_thing_id = overrides.pop("virtual_thing_id", "virtual:records:demo")

    if action_kind == JobActionKind.ANALYSIS:
        action = {"kind": "analysis", "analysis_code": analysis_code}
    else:
        action = {"kind": "prompt", "prompt": prompt}

    if trigger_kind == JobTriggerKind.EVENT:
        trigger = {
            "kind": "event",
            "thing_id": thing_id,
            "event_name": event_name,
            "subscription_input": subscription_input,
        }
    elif schedule_kind == TimeTriggerKind.ONCE:
        trigger = {"kind": "time", "schedule": {"kind": "once", "run_at": run_at or now}}
    elif schedule_kind == TimeTriggerKind.CRON:
        trigger = {
            "kind": "time",
            "schedule": {
                "kind": "cron",
                "expression": cron_expression or "0 9 * * sun",
                "timezone": cron_timezone,
            },
        }
    else:
        trigger = {
            "kind": "time",
            "schedule": {
                "kind": "interval",
                "interval_seconds": interval_seconds or 60,
            },
        }

    output = (
        {
            "kind": "structured_record",
            "schema": record_schema,
            "schema_version": record_schema_version,
            "virtual_thing": {"id": virtual_thing_id},
        }
        if output_kind == JobOutputKind.STRUCTURED_RECORD
        else {"kind": "narrative"}
    )
    values = {
        "id": "job-1",
        "name": "Demo job",
        "created_from_thread_id": "thread-1",
        "job_thread_id": "job:job-1",
        "action": action,
        "output": output,
        "enabled": True,
        "trigger": trigger,
        "next_run_at": now,
        "subscription_id": None,
        "created_at": now,
        "updated_at": now,
        "last_run_id": None,
        "last_run_at": None,
        "last_run_status": None,
        "last_error": None,
        "last_response": None,
        "run_count": 0,
    }
    values.update(overrides)
    return Job(**values)


def _run(**overrides) -> JobRun:
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    values = {
        "id": "run-1",
        "job_id": "job-1",
        "job_thread_id": "job:job-1:run:run-1",
        "source": JobRunSource.MANUAL,
        "status": JobRunStatus.RUNNING,
        "trigger_payload": {"source": "manual"},
        "started_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return JobRun(**values)


class JobGraphResultsTestCase(unittest.TestCase):
    def test_reply_to_pending_interrupt_uses_command_resume(self) -> None:
        graph_input = graph_input_for_run(
            _job(),
            _run(result={"metadata": {"pending_interrupt": True}}),
            "21 C",
        )

        self.assertIsInstance(graph_input, Command)
        self.assertEqual(graph_input.resume, "21 C")

    def test_waiting_question_prefers_interrupt_payload(self) -> None:
        result = {
            "messages": [AIMessage(content="", tool_calls=[])],
            "__interrupt__": [{"value": {"question": "Which production line?"}}],
        }

        self.assertEqual(waiting_question_from_graph_result(result), "Which production line?")

    def test_parse_graph_result_collects_policy_inputs(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="plot and record"),
                ToolMessage(
                    content=(
                        '{"stdout": "created plot", "artifacts": ['
                        '{"ref": "plot", "kind": "image", "filename": "plot.png"}]}'
                    ),
                    name="run_code",
                    tool_call_id="call-1",
                ),
                ToolMessage(
                    content='{"ok": true, "record": {"data": {"mood": "good"}}}',
                    name="submit_job_record",
                    tool_call_id="call-2",
                ),
                AIMessage(content="Stored with a chart."),
            ]
        }

        parsed = parse_graph_result(result)

        self.assertEqual(parsed.assistant, "Stored with a chart.")
        self.assertIsNone(parsed.waiting_question)
        self.assertEqual(parsed.submitted_record["data"], {"mood": "good"})
        self.assertIsNone(parsed.failed_record_submission)
        self.assertEqual(
            parsed.code_result,
            {
                "artifacts": [{"ref": "image_1", "kind": "image", "filename": "plot.png"}],
                "stdout": "created plot",
            },
        )
        self.assertFalse(parsed.has_interrupt)

    def test_resumed_ask_tool_result_is_not_waiting_again(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="start"),
                ToolMessage(
                    content=(
                        '{"status": "input_received", "question": "Which production line?", '
                        '"answer": "line 3"}'
                    ),
                    name="ask_job_user",
                    tool_call_id="call-1",
                ),
                AIMessage(content="done"),
            ]
        }

        self.assertIsNone(waiting_question_from_graph_result(result))

    def test_submitted_record_and_status_are_classified(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="start"),
                ToolMessage(
                    content='{"ok": true, "record": {"data": {"mood": "good"}}}',
                    name="submit_job_record",
                    tool_call_id="call-1",
                ),
                AIMessage(content="Stored."),
            ]
        }

        submitted_record = submitted_record_from_graph_result(result)
        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.STRUCTURED_RECORD),
            message="good",
            trigger={"source": "user_reply"},
        )

        self.assertEqual(submitted_record["data"], {"mood": "good"})
        self.assertTrue(job_result["ok"])
        self.assertEqual(job_result["submitted_record"]["data"], {"mood": "good"})
        self.assertEqual(job_run_status_from_result(job_result), JobRunStatus.SUCCEEDED)

    def test_autonomous_record_prompt_completes_when_record_is_submitted(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="Observe and store mood"),
                ToolMessage(
                    content='{"ok": true, "record": {"data": {"mood": "good"}}}',
                    name="submit_job_record",
                    tool_call_id="call-1",
                ),
                AIMessage(content="Stored."),
            ]
        }

        job_result = job_result_from_graph_result(
            result,
            job=_job(
                interaction_mode=JobInteractionMode.AUTONOMOUS,
                output_kind=JobOutputKind.STRUCTURED_RECORD,
            ),
            message=None,
            trigger={"source": "manual"},
        )

        self.assertTrue(job_result["ok"])
        self.assertEqual(job_result["submitted_record"]["data"], {"mood": "good"})
        self.assertEqual(job_run_status_from_result(job_result), JobRunStatus.SUCCEEDED)

    def test_run_code_artifacts_are_promoted_for_prompt_jobs(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="plot the temperatures"),
                ToolMessage(
                    content=(
                        '{"stdout": "created plot", "artifacts": ['
                        '{"ref": "image_1", "kind": "image", "filename": "plot.png"}]}'
                    ),
                    name="run_code",
                    tool_call_id="call-1",
                ),
                AIMessage(content="A plot was generated."),
            ]
        }

        code_result = code_result_from_graph_result(result)
        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.NARRATIVE),
            message=None,
            trigger={"source": "manual"},
        )

        self.assertEqual(
            code_result,
            {
                "artifacts": [{"ref": "image_1", "kind": "image", "filename": "plot.png"}],
                "stdout": "created plot",
            },
        )
        self.assertEqual(job_result["artifacts"], code_result["artifacts"])
        self.assertEqual(job_result["stdout"], "created plot")

    def test_file_exports_preserve_metadata_and_deduplicate_by_id(self) -> None:
        import json

        first = {
            "id": "file-first.csv",
            "ref": "file_1",
            "kind": "file",
            "filename": "data.csv",
            "size_bytes": 8,
            "sha256": "digest",
            "expires_at": "2026-09-15T12:00:00Z",
        }
        second = {**first, "id": "file-second.csv"}
        result = {
            "messages": [
                ToolMessage(
                    content=json.dumps({"artifacts": [first, second, first]}),
                    name="run_code",
                    tool_call_id="files",
                ),
                AIMessage(content="Two exports are ready."),
            ]
        }
        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.NARRATIVE),
            message=None,
            trigger={"source": "manual"},
        )
        assert job_result["artifacts"] == [first, {**second, "ref": "file_2"}]

    def test_run_code_stdout_only_is_not_promoted_for_prompt_jobs(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="summarize the temperatures"),
                ToolMessage(
                    content='{"stdout": "22.9 C"}',
                    name="run_code",
                    tool_call_id="call-1",
                ),
                AIMessage(content="The temperature is 22.9 C."),
            ]
        }

        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.NARRATIVE),
            message=None,
            trigger={"source": "manual"},
        )

        self.assertIsNone(code_result_from_graph_result(result))
        self.assertNotIn("stdout", job_result)
        self.assertNotIn("artifacts", job_result)

    def test_failed_record_schema_submission_waits_for_repair(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="energy was very high"),
                ToolMessage(
                    content=(
                        '{"ok": false, "error": "record data failed schema validation '
                        "at energy: 'high' is not of type 'integer'\"}"
                    ),
                    name="submit_job_record",
                    tool_call_id="call-1",
                ),
                AIMessage(content="I could not store the record."),
            ]
        }

        failed_submission = failed_record_submission_from_graph_result(result)
        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.STRUCTURED_RECORD),
            message="energy was very high",
            trigger={"source": "user_reply"},
        )

        self.assertIsNotNone(failed_submission)
        self.assertTrue(job_result["ok"])
        self.assertEqual(job_result["status"], JobRunStatus.WAITING_FOR_INPUT.value)
        self.assertIn("energy", job_result["waiting_question"])
        self.assertIn("record_submission_error", job_result["metadata"])
        self.assertEqual(job_run_status_from_result(job_result), JobRunStatus.WAITING_FOR_INPUT)

    def test_repairable_record_tool_error_waits_for_repair(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="energy was very high"),
                ToolMessage(
                    content='{"ok": false, "repairable": true, "error": "missing energy"}',
                    name="submit_job_record",
                    tool_call_id="call-1",
                ),
                AIMessage(content="I could not store the record."),
            ]
        }

        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.STRUCTURED_RECORD),
            message="energy was very high",
            trigger={"source": "user_reply"},
        )

        self.assertTrue(job_result["ok"])
        self.assertEqual(job_result["status"], JobRunStatus.WAITING_FOR_INPUT.value)
        self.assertIn("missing energy", job_result["waiting_question"])

    def test_non_user_repairable_record_tool_error_still_fails(self) -> None:
        result = {
            "messages": [
                HumanMessage(content="good"),
                ToolMessage(
                    content=(
                        '{"ok": false, "error": '
                        '"submit_job_record requires job_id, run_id, and virtual_thing_id"}'
                    ),
                    name="submit_job_record",
                    tool_call_id="call-1",
                ),
                AIMessage(content="I could not store the record."),
            ]
        }

        job_result = job_result_from_graph_result(
            result,
            job=_job(output_kind=JobOutputKind.STRUCTURED_RECORD),
            message="good",
            trigger={"source": "user_reply"},
        )

        self.assertFalse(job_result["ok"])
        self.assertEqual(job_run_status_from_result(job_result), JobRunStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
