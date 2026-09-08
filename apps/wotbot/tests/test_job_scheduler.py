import unittest
from datetime import datetime, timezone

from wotbot.agent.tools import job_scheduler
from wotbot.jobs.models import (
    CreateJobRequest,
    Job,
    JobActionKind,
    JobOutputKind,
    JobTriggerKind,
    TimeTriggerKind,
)
from wotbot.jobs.active import set_active_job_service


def _job(**overrides) -> Job:
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    action_kind = overrides.pop("action_kind", JobActionKind.PROMPT)
    prompt = overrides.pop("prompt", "check")
    analysis_code = overrides.pop("analysis_code", "print('ok')")
    output_kind = overrides.pop("output_kind", JobOutputKind.NARRATIVE)
    trigger_kind = overrides.pop("trigger_kind", JobTriggerKind.TIME)
    schedule_kind = overrides.pop("schedule_kind", TimeTriggerKind.INTERVAL)
    run_at = overrides.pop("run_at", None)
    interval_seconds = overrides.pop("interval_seconds", 10)
    cron_expression = overrides.pop("cron_expression", None)
    cron_timezone = overrides.pop("cron_timezone", None)

    action = (
        {"kind": "analysis", "analysis_code": analysis_code}
        if action_kind == JobActionKind.ANALYSIS
        else {"kind": "prompt", "prompt": prompt}
    )
    if trigger_kind == JobTriggerKind.EVENT:
        trigger = {
            "kind": "event",
            "thing_id": overrides.pop("thing_id", "thing-1"),
            "event_name": overrides.pop("event_name", "changed"),
            "subscription_input": overrides.pop("subscription_input", None),
        }
    elif schedule_kind == TimeTriggerKind.CRON:
        trigger = {
            "kind": "time",
            "schedule": {
                "kind": "cron",
                "expression": cron_expression or "0 9 * * sun",
                "timezone": cron_timezone,
            },
        }
    elif schedule_kind == TimeTriggerKind.ONCE:
        trigger = {"kind": "time", "schedule": {"kind": "once", "run_at": run_at or now}}
    else:
        trigger = {
            "kind": "time",
            "schedule": {"kind": "interval", "interval_seconds": interval_seconds or 10},
        }
    output = (
        {
            "kind": "structured_record",
            "schema": overrides.pop(
                "record_schema",
                {"type": "object", "properties": {"mood": {"type": "string"}}},
            ),
            "schema_version": overrides.pop("record_schema_version", 1),
            "virtual_thing": {"id": overrides.pop("virtual_thing_id", "virtual:records:demo")},
        }
        if output_kind == JobOutputKind.STRUCTURED_RECORD
        else {"kind": "narrative"}
    )
    values = {
        "id": "job-123",
        "name": "demo",
        "created_from_thread_id": "thread-1",
        "job_thread_id": "job:job-123",
        "action": action,
        "output": output,
        "enabled": True,
        "trigger": trigger,
        "next_run_at": now,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return Job(**values)


class _FakeService:
    def __init__(self, *, run_result=None, create_error=None) -> None:
        self._run_result = run_result or {"ok": True, "response": "done"}
        self._create_error = create_error
        self.created_requests: list[CreateJobRequest] = []
        self.deleted: list[str] = []
        self.ran: list[str] = []
        self.listed: list[str | None] = []

    async def create_job(self, request: CreateJobRequest) -> Job:
        if self._create_error is not None:
            raise self._create_error
        self.created_requests.append(request)
        return _job(
            name=request.name,
            created_from_thread_id=request.created_from_thread_id,
            action=request.action,
            interaction_mode=request.interaction_mode,
            output=request.output,
            trigger=request.trigger,
        )

    async def run_job_now(self, job_id: str) -> dict:
        self.ran.append(job_id)
        return self._run_result

    async def delete_job(self, job_id: str) -> Job:
        self.deleted.append(job_id)
        return _job(id=job_id)

    async def list_jobs(self, created_from_thread_id: str | None = None) -> list[Job]:
        self.listed.append(created_from_thread_id)
        return [_job()]


class JobSchedulerTestCase(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        set_active_job_service(None)

    async def test_prompt_job_tools_expose_runtime_instructions_argument(self) -> None:
        self.assertIn("run_instructions", job_scheduler.create_prompt_job.args)
        self.assertNotIn("prompt", job_scheduler.create_prompt_job.args)
        self.assertIn("run_instructions", job_scheduler.create_record_prompt_job.args)
        self.assertNotIn("prompt", job_scheduler.create_record_prompt_job.args)

    async def test_create_analysis_job_returns_created_job_without_running(self) -> None:
        service = _FakeService(run_result={"ok": False, "error": "boom"})
        set_active_job_service(service)

        result = await job_scheduler.create_analysis_job.ainvoke(
            {
                "name": "demo job",
                "analysis_code": "print('hello')",
                "trigger_kind": "time",
                "schedule_kind": "interval",
                "interval_seconds": 60,
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result["id"], "job-123")
        self.assertNotIn("test_run", result)
        self.assertEqual(service.ran, [])
        self.assertEqual(service.deleted, [])
        self.assertEqual(service.created_requests[0].action.kind, "analysis")

    async def test_create_analysis_job_with_record_schema_emits_structured_output(self) -> None:
        service = _FakeService()
        set_active_job_service(service)

        schema = {
            "type": "object",
            "properties": {"avg_temp": {"type": "number"}},
            "required": ["avg_temp"],
        }
        result = await job_scheduler.create_analysis_job.ainvoke(
            {
                "name": "daily avg temp",
                "analysis_code": "store_record({'avg_temp': 21.5})",
                "trigger_kind": "time",
                "schedule_kind": "interval",
                "interval_seconds": 3600,
                "record_schema": schema,
                "virtual_thing_title": "Daily Avg Temp",
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result["id"], "job-123")
        request = service.created_requests[0]
        self.assertEqual(request.action.kind, "analysis")
        self.assertEqual(request.output.kind, "structured_record")
        self.assertEqual(request.output.schema, schema)
        self.assertEqual(request.output.virtual_thing.title, "Daily Avg Temp")

    async def test_create_analysis_job_without_record_schema_stays_narrative(self) -> None:
        service = _FakeService()
        set_active_job_service(service)

        await job_scheduler.create_analysis_job.ainvoke(
            {
                "name": "demo job",
                "analysis_code": "print('hello')",
                "trigger_kind": "time",
                "schedule_kind": "interval",
                "interval_seconds": 60,
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(service.created_requests[0].output.kind, "narrative")

    async def test_create_prompt_job_returns_created_job_without_running(self) -> None:
        service = _FakeService(run_result={"ok": True, "response": "ran"})
        set_active_job_service(service)

        result = await job_scheduler.create_prompt_job.ainvoke(
            {
                "name": "demo job",
                "run_instructions": "check the production queue",
                "trigger_kind": "time",
                "schedule_kind": "interval",
                "interval_seconds": 10,
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result["id"], "job-123")
        self.assertNotIn("test_run", result)
        self.assertEqual(service.created_requests[0].created_from_thread_id, "thread-1")
        schedule = service.created_requests[0].trigger.schedule
        self.assertEqual(schedule.kind, "interval")
        self.assertEqual(schedule.interval_seconds, 10)
        self.assertEqual(service.ran, [])
        self.assertEqual(service.deleted, [])

    async def test_create_prompt_job_passes_cron_schedule(self) -> None:
        service = _FakeService()
        set_active_job_service(service)

        result = await job_scheduler.create_prompt_job.ainvoke(
            {
                "name": "weekly check",
                "run_instructions": "Check the production queue.",
                "trigger_kind": "time",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * sun",
                "cron_timezone": "Europe/Berlin",
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result["trigger"]["schedule"]["kind"], "cron")
        request = service.created_requests[0]
        self.assertEqual(request.trigger.schedule.kind, "cron")
        self.assertEqual(request.trigger.schedule.expression, "0 9 * * sun")
        self.assertEqual(request.trigger.schedule.timezone, "Europe/Berlin")

    async def test_create_record_prompt_job_passes_schema_contract(self) -> None:
        service = _FakeService()
        set_active_job_service(service)

        result = await job_scheduler.create_record_prompt_job.ainvoke(
            {
                "name": "morning check-in",
                "run_instructions": "Ask how I feel and store mood.",
                "record_schema": {
                    "type": "object",
                    "properties": {"mood": {"type": "string"}},
                    "required": ["mood"],
                },
                "trigger_kind": "time",
                "schedule_kind": "interval",
                "interval_seconds": 86400,
                "virtual_thing_title": "Morning Check-ins",
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result["output"]["kind"], "structured_record")
        request = service.created_requests[0]
        self.assertEqual(request.output.kind, JobOutputKind.STRUCTURED_RECORD.value)
        self.assertEqual(request.output.schema["required"], ["mood"])
        self.assertEqual(request.output.virtual_thing.title, "Morning Check-ins")

    async def test_create_prompt_job_reports_validation_error(self) -> None:
        service = _FakeService(
            create_error=ValueError("time jobs require run_at or interval_seconds")
        )
        set_active_job_service(service)

        result = await job_scheduler.create_prompt_job.ainvoke(
            {
                "name": "demo job",
                "run_instructions": "check",
                "trigger_kind": "time",
            },
            config={"configurable": {"thread_id": "thread-1"}},
        )

        self.assertEqual(result, {"error": "time jobs require run_at or interval_seconds"})

    async def test_tools_report_when_service_unavailable(self) -> None:
        set_active_job_service(None)

        result = await job_scheduler.list_jobs.ainvoke({})

        self.assertEqual(result, {"error": "Job service is not ready"})

    async def test_list_delete_run_delegate_to_service(self) -> None:
        service = _FakeService(run_result={"ok": True})
        set_active_job_service(service)

        listed = await job_scheduler.list_jobs.ainvoke({"created_from_thread_id": "thread-1"})
        deleted = await job_scheduler.delete_job.ainvoke({"job_id": "job-9"})
        ran = await job_scheduler.run_job_now.ainvoke({"job_id": "job-9"})

        self.assertEqual(listed["jobs"][0]["id"], "job-123")
        self.assertEqual(service.listed, ["thread-1"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(service.deleted, ["job-9"])
        self.assertEqual(ran, {"ok": True})
        self.assertEqual(service.ran, ["job-9"])


if __name__ == "__main__":
    unittest.main()
