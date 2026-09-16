from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.job_helpers import create_job_request
from wotbot.core.settings import Settings
from wotbot.jobs.executor import JobExecutor
from wotbot.jobs.graph_results import job_result_from_graph_result
from wotbot.jobs.models import (
    JobInteractionMode,
    JobOutputKind,
    JobRunEventType,
    JobRunSource,
    JobRunStatus,
    JobTriggerKind,
    TimeTriggerKind,
    UpdateJobRequest,
)
from wotbot.jobs.records import VirtualRecordStore, make_virtual_record_thing_id
from wotbot.jobs.resources import JobResourceManager
from wotbot.jobs.results import JobRunEventPublisher
from wotbot.jobs.routes import router as jobs_router
from wotbot.jobs.schedule import build_schedule_source, schedule_id_for_job
from wotbot.jobs.service import JobService
from wotbot.jobs.stores import JobStore, utc_now
from wotbot.threads.store import get_thread, list_threads

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.run(coro)


def _get_schedules(settings: Settings):
    return _run(build_schedule_source(settings).get_schedules())


def _build_jobs_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.service = JobService(settings)
    app.include_router(jobs_router)
    return app


class _NoopScheduleManager:
    async def add_job(self, job) -> None:
        return None

    async def remove_job(self, job_id: str) -> None:
        return None

    async def sync(self) -> None:
        return None


class _NoopRuntimeClient:
    async def subscribe_event(self, **kwargs):
        return {"subscription": {"subscriptionId": "sub-1"}}

    async def remove_subscription(self, *, subscription_id: str) -> None:
        return None


class _RecordingRuntimeClient:
    def __init__(self) -> None:
        self.subscribed = []
        self.removed = []

    async def subscribe_event(self, **kwargs):
        self.subscribed.append(kwargs)
        return {"subscription": {"subscriptionId": "new-sub"}}

    async def remove_subscription(self, *, subscription_id: str) -> None:
        self.removed.append(subscription_id)


class _NoopJobRunPublisher:
    def __init__(self) -> None:
        self.published = []

    async def publish_job_run(self, job_id: str, *, run_id: str | None = None) -> None:
        self.published.append((job_id, run_id))


def test_jobs_api_persists_time_job_and_syncs_redis_schedule(
    jobs_integration_environment,
) -> None:
    settings = Settings(
        redis_url=jobs_integration_environment.redis_url,
        internal_api_key="",
    )
    app = _build_jobs_app(settings)

    with TestClient(app) as client:
        create_response = client.post(
            "/jobs",
            json={
                "name": "check doors",
                "created_from_thread_id": "thread-1",
                "action": {"kind": "prompt", "prompt": "Check all exterior doors"},
                "trigger": {
                    "kind": "time",
                    "schedule": {"kind": "interval", "interval_seconds": 60},
                },
                "output": {"kind": "narrative"},
            },
        )

        assert create_response.status_code == 200
        created = create_response.json()
        assert created["name"] == "check doors"
        assert created["created_from_thread_id"] == "thread-1"
        assert created["job_thread_id"] == f"job:{created['id']}"
        assert created["trigger"]["kind"] == "time"
        assert created["trigger"]["schedule"]["kind"] == "interval"
        assert created["action"]["kind"] == "prompt"
        assert created["output"]["kind"] == "narrative"
        assert "trigger_kind" not in created
        assert "action_kind" not in created
        assert "schedule_kind" not in created
        assert "prompt" not in created
        assert created["enabled"] is True
        assert created["next_run_at"] is not None
        assert [thread["id"] for thread in list_threads()] == []
        hidden_thread = get_thread(created["job_thread_id"])
        assert hidden_thread is not None
        assert hidden_thread["kind"] == "job"
        assert hidden_thread["visible"] is False
        assert hidden_thread["jobId"] == created["id"]

        list_response = client.get("/jobs", params={"created_from_thread_id": "thread-1"})
        assert list_response.status_code == 200
        assert [job["id"] for job in list_response.json()["jobs"]] == [created["id"]]

        schedules = _get_schedules(settings)
        assert [schedule.schedule_id for schedule in schedules] == [
            schedule_id_for_job(created["id"])
        ]
        assert schedules[0].interval == 60

        delete_response = client.delete(f"/jobs/{created['id']}")
        assert delete_response.status_code == 200

        schedules = _get_schedules(settings)
        assert schedules == []

        get_response = client.get(f"/jobs/{created['id']}")
        assert get_response.status_code == 404


def test_job_run_event_publisher_writes_current_job_snapshot_to_redis_stream(
    jobs_integration_environment,
) -> None:
    settings = Settings(redis_url=jobs_integration_environment.redis_url)
    repo = JobStore()
    now = utc_now()
    job = _run(
        repo.create_job(
            create_job_request(
                name="daily check",
                created_from_thread_id="thread-2",
                prompt="summarize system status",
                trigger_kind=JobTriggerKind.TIME,
                schedule_kind=TimeTriggerKind.INTERVAL,
                interval_seconds=300,
            ),
            next_run_at=now,
            subscription_id=None,
        )
    )
    run = _run(
        repo.try_start_job_run(
            job_id=job.id,
            source=JobRunSource.TIME,
            trigger_payload={"source": "time"},
            now=now + timedelta(seconds=1),
        )
    )
    assert run is not None
    _run(
        repo.finish_job_run(
            run_id=run.id,
            job_id=job.id,
            now=now + timedelta(seconds=1),
            status=JobRunStatus.SUCCEEDED,
            error=None,
            response_text="all clear",
            result={"ok": True, "assistant": "all clear"},
            next_run_at=now + timedelta(seconds=301),
        )
    )
    runs = _run(repo.list_job_runs(job.id))
    assert [job_run.id for job_run in runs] == [run.id]

    event_id = _run(
        JobRunEventPublisher(settings, repo=repo).publish_job_run(job.id, run_id=run.id)
    )

    assert event_id is not None
    redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        records = redis_client.xrange(settings.jobs_run_events_stream)
    finally:
        redis_client.close()

    assert len(records) == 1
    payload = json.loads(records[0][1]["payload"])
    assert payload["type"] == "job_run"
    assert payload["job"]["id"] == job.id
    assert payload["job"]["last_response"] == "all clear"
    assert payload["job"]["run_count"] == 1
    assert payload["run"]["id"] == run.id
    assert payload["run"]["status"] == "succeeded"


def test_virtual_record_store_persists_and_queries_generated_record_thing(
    jobs_integration_environment,
) -> None:
    repo = JobStore()
    records = VirtualRecordStore()
    now = utc_now()
    thing_id = make_virtual_record_thing_id("morning wellbeing")
    schema = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "enum": ["good", "stressed"]},
            "energy": {"type": "integer", "minimum": 1, "maximum": 5},
            "note": {"type": "string"},
        },
        "required": ["mood", "energy"],
    }
    job = _run(
        repo.create_job(
            create_job_request(
                name="morning wellbeing",
                created_from_thread_id="thread-3",
                interaction_mode=JobInteractionMode.REQUIRED_CHECKIN,
                output_kind=JobOutputKind.STRUCTURED_RECORD,
                prompt="Ask how I feel.",
                record_schema=schema,
                record_schema_version=1,
                virtual_thing_id=thing_id,
                trigger_kind=JobTriggerKind.TIME,
                schedule_kind=TimeTriggerKind.INTERVAL,
                interval_seconds=86400,
            ),
            next_run_at=now,
            subscription_id=None,
        )
    )
    records.create_or_update_thing(
        thing_id=thing_id,
        source_job_id=job.id,
        schema_version=1,
        record_schema=schema,
        title="Morning Wellbeing",
        description="Daily wellbeing check-ins.",
    )
    run = _run(
        repo.try_start_job_run(
            job_id=job.id,
            source=JobRunSource.TIME,
            trigger_payload={"source": "time"},
            now=now,
        )
    )
    assert run is not None

    stored = records.submit_record(
        thing_id=thing_id,
        source_job_id=job.id,
        source_run_id=run.id,
        data={"mood": "stressed", "energy": 2, "note": "slept badly"},
    )

    assert stored["data"]["mood"] == "stressed"
    assert records.read_property(thing_id, "latest_mood") == "stressed"
    assert records.read_property(thing_id, "record_count") == 1
    assert (
        records.invoke_action(
            thing_id,
            "query_property_history",
            {"property": "energy"},
        )[0]["value"]
        == 2
    )
    # The per-field history action resolves without a `property` argument.
    assert records.invoke_action(thing_id, "history_energy", {})[0]["value"] == 2


def test_job_resource_sync_repairs_missing_virtual_record_thing(
    jobs_integration_environment,
) -> None:
    repo = JobStore()
    records = VirtualRecordStore()
    now = utc_now()
    thing_id = make_virtual_record_thing_id("repaired wellbeing")
    schema = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "enum": ["good", "stressed"]},
            "energy": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["mood", "energy"],
    }
    job = _run(
        repo.create_job(
            create_job_request(
                name="repaired wellbeing",
                created_from_thread_id="thread-4",
                interaction_mode=JobInteractionMode.REQUIRED_CHECKIN,
                output_kind=JobOutputKind.STRUCTURED_RECORD,
                prompt="Ask how I feel.",
                record_schema=schema,
                record_schema_version=1,
                virtual_thing_id=thing_id,
                trigger_kind=JobTriggerKind.TIME,
                schedule_kind=TimeTriggerKind.INTERVAL,
                interval_seconds=86400,
            ),
            next_run_at=now,
            subscription_id=None,
        )
    )
    manager = JobResourceManager(
        repo=repo,
        runtime_client=_NoopRuntimeClient(),
        schedule_manager=_NoopScheduleManager(),
        record_store=records,
    )

    assert records.thing_exists(thing_id) is False

    repaired = _run(manager.sync_record_things())

    assert repaired == 1
    assert records.thing_exists(thing_id) is True
    assert records.read_property(thing_id, "record_count") == 0
    assert records.read_property(thing_id, "latest_mood") is None
    assert _run(repo.get_job(job.id)).output.virtual_thing_id == thing_id


def test_job_resource_sync_replaces_event_subscription_id(
    jobs_integration_environment,
) -> None:
    repo = JobStore()
    runtime_client = _RecordingRuntimeClient()
    job = _run(
        repo.create_job(
            create_job_request(
                name="overheat watcher",
                created_from_thread_id="thread-5",
                prompt="Summarize the event payload.",
                trigger_kind=JobTriggerKind.EVENT,
                thing_id="urn:test:heater",
                event_name="overheated",
                subscription_input={"threshold": 70},
            ),
            next_run_at=None,
            subscription_id="old-sub",
        )
    )
    manager = JobResourceManager(
        repo=repo,
        runtime_client=runtime_client,
        schedule_manager=_NoopScheduleManager(),
        record_store=VirtualRecordStore(),
    )

    synced = _run(manager.sync_event_subscriptions())

    updated = _run(repo.get_job(job.id))
    assert synced == 1
    assert runtime_client.removed == ["old-sub"]
    assert runtime_client.subscribed == [
        {
            "thing_id": "urn:test:heater",
            "event_name": "overheated",
            "subscription_input": {"threshold": 70},
        }
    ]
    assert updated.subscription_id == "new-sub"


def test_event_job_toggle_updates_runtime_subscription(
    jobs_integration_environment,
) -> None:
    repo = JobStore()
    runtime_client = _RecordingRuntimeClient()
    job = _run(
        repo.create_job(
            create_job_request(
                name="toggle watcher",
                created_from_thread_id="thread-6",
                prompt="Summarize the event payload.",
                trigger_kind=JobTriggerKind.EVENT,
                thing_id="urn:test:heater",
                event_name="overheated",
            ),
            next_run_at=None,
            subscription_id="old-sub",
        )
    )
    service = JobService(
        Settings(redis_url=jobs_integration_environment.redis_url),
        repo=repo,
        runtime_client=runtime_client,
        schedule_manager=_NoopScheduleManager(),
    )

    disabled = _run(service.update_job(job.id, UpdateJobRequest(enabled=False)))
    reenabled = _run(service.update_job(job.id, UpdateJobRequest(enabled=True)))

    assert disabled.enabled is False
    assert disabled.subscription_id is None
    assert runtime_client.removed == ["old-sub"]
    assert runtime_client.subscribed == [
        {
            "thing_id": "urn:test:heater",
            "event_name": "overheated",
            "subscription_input": None,
        }
    ]
    assert reenabled.enabled is True
    assert reenabled.subscription_id == "new-sub"
    stored = _run(repo.get_job(job.id))
    assert stored.subscription_id == "new-sub"
    assert stored.resource_health["status"] == "healthy"
    assert stored.resource_health["resources"]["event_subscription"]["status"] == "healthy"


def test_structured_record_reply_replay_writes_one_reply_event_and_record(
    jobs_integration_environment,
) -> None:
    settings = Settings(
        redis_url=jobs_integration_environment.redis_url,
        internal_api_key="",
    )
    service = JobService(settings)
    repo = JobStore()
    records = VirtualRecordStore()
    now = utc_now()
    thing_id = make_virtual_record_thing_id("evening wellbeing")
    schema = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "enum": ["good", "stressed"]},
            "energy": {"type": "integer", "minimum": 1, "maximum": 5},
            "note": {"type": "string"},
        },
        "required": ["mood", "energy"],
    }
    job = _run(
        service.create_job(
            create_job_request(
                name="evening wellbeing",
                created_from_thread_id="thread-4",
                interaction_mode=JobInteractionMode.REQUIRED_CHECKIN,
                output_kind=JobOutputKind.STRUCTURED_RECORD,
                prompt="Ask for the user's evening wellbeing.",
                record_schema=schema,
                record_schema_version=1,
                virtual_thing_id=thing_id,
                virtual_thing_title="Evening Wellbeing",
                trigger_kind=JobTriggerKind.TIME,
                schedule_kind=TimeTriggerKind.INTERVAL,
                interval_seconds=86400,
            )
        )
    )
    run = _run(
        repo.try_start_job_run(
            job_id=job.id,
            source=JobRunSource.MANUAL,
            trigger_payload={"source": "manual"},
            now=now,
        )
    )
    assert run is not None
    _run(
        repo.finish_job_run(
            run_id=run.id,
            job_id=job.id,
            now=now + timedelta(seconds=1),
            status=JobRunStatus.WAITING_FOR_INPUT,
            error=None,
            response_text="How was your evening?",
            result={"ok": True, "status": "waiting_for_input"},
            waiting_question="How was your evening?",
        )
    )

    first_reply = _run(
        repo.start_reply_job_run(
            job_id=job.id,
            message="Good mood, energy 4. I cooked dinner.",
            client_reply_id="reply-evening-1",
            previous_run_id=run.id,
            now=now + timedelta(seconds=2),
        )
    )
    submitted = records.submit_record(
        thing_id=thing_id,
        source_job_id=job.id,
        source_run_id=first_reply.id,
        data={"mood": "good", "energy": 4, "note": "cooked dinner"},
        raw_input="Good mood, energy 4. I cooked dinner.",
        confidence=0.93,
    )
    duplicate_reply = _run(
        repo.start_reply_job_run(
            job_id=job.id,
            message="Good mood, energy 4. I cooked dinner.",
            client_reply_id="reply-evening-1",
            previous_run_id=run.id,
            now=now + timedelta(seconds=3),
        )
    )
    duplicate_record = records.submit_record(
        thing_id=thing_id,
        source_job_id=job.id,
        source_run_id=duplicate_reply.id,
        data={"mood": "stressed", "energy": 1, "note": "duplicate parse"},
    )
    _run(
        repo.finish_job_run(
            run_id=first_reply.id,
            job_id=job.id,
            now=now + timedelta(seconds=4),
            status=JobRunStatus.SUCCEEDED,
            error=None,
            response_text="Recorded.",
            result={
                "ok": True,
                "assistant": "Recorded.",
                "submitted_record": submitted,
            },
        )
    )

    assert duplicate_reply.id == first_reply.id
    assert duplicate_reply.trigger_payload["_duplicate_reply"] is True
    assert len(duplicate_reply.trigger_payload["replies"]) == 1
    assert duplicate_record["id"] == submitted["id"]
    assert duplicate_record["data"] == {"mood": "good", "energy": 4, "note": "cooked dinner"}
    assert records.read_property(thing_id, "record_count") == 1
    assert records.read_property(thing_id, "latest_mood") == "good"

    events = _run(repo.list_job_run_events(job.id))
    event_types = [event.event_type for event in events]
    assert event_types == [
        JobRunEventType.RUN_STARTED,
        JobRunEventType.WAITING_FOR_INPUT,
        JobRunEventType.USER_REPLY,
        JobRunEventType.RECORD_SUBMITTED,
        JobRunEventType.ASSISTANT_MESSAGE,
        JobRunEventType.RUN_SUCCEEDED,
    ]
    assert [
        event.message for event in events if event.event_type == JobRunEventType.USER_REPLY
    ] == ["Good mood, energy 4. I cooked dinner."]
    assert events[2].payload["client_reply_id"] == "reply-evening-1"
    assert (
        events[3].message == "Structured record submitted: mood=good, energy=4, note=cooked dinner"
    )
    assert events[3].payload["data"]["mood"] == "good"


def test_structured_record_bad_answer_waits_for_repair_then_completes(
    jobs_integration_environment,
) -> None:
    repo = JobStore()
    records = VirtualRecordStore()
    now = utc_now() - timedelta(minutes=10)
    thing_id = make_virtual_record_thing_id("repair wellbeing")
    schema = {
        "type": "object",
        "required": ["mood", "energy"],
        "properties": {
            "mood": {"type": "string", "enum": ["good", "stressed"]},
            "energy": {"type": "integer", "minimum": 1, "maximum": 5},
            "note": {"type": "string"},
        },
        "additionalProperties": False,
    }
    job = _run(
        repo.create_job(
            create_job_request(
                name="repair wellbeing",
                created_from_thread_id="thread-repair",
                prompt="Ask for a short wellbeing check-in and store it.",
                interaction_mode=JobInteractionMode.REQUIRED_CHECKIN,
                output_kind=JobOutputKind.STRUCTURED_RECORD,
                record_schema=schema,
                record_schema_version=1,
                virtual_thing_id=thing_id,
                trigger_kind=JobTriggerKind.TIME,
                schedule_kind=TimeTriggerKind.INTERVAL,
                interval_seconds=86400,
            ),
            next_run_at=now,
            subscription_id=None,
        )
    )
    records.create_or_update_thing(
        thing_id=thing_id,
        source_job_id=job.id,
        schema_version=1,
        record_schema=schema,
        title="Repair Wellbeing",
        description="Structured records collected during the repair-loop test.",
    )
    run = _run(
        repo.try_start_job_run(
            job_id=job.id,
            source=JobRunSource.MANUAL,
            trigger_payload={"source": "manual"},
            now=now,
        )
    )
    assert run is not None
    _run(
        repo.finish_job_run(
            run_id=run.id,
            job_id=job.id,
            now=now + timedelta(seconds=1),
            status=JobRunStatus.WAITING_FOR_INPUT,
            error=None,
            response_text="How was your wellbeing today?",
            result={"ok": True, "status": "waiting_for_input"},
            waiting_question="How was your wellbeing today?",
        )
    )

    class _RepairLoopAgentRunner:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, job, *, run, trigger):
            self.calls.append((job, run, trigger))
            if len(self.calls) == 1:
                graph_result = {
                    "messages": [
                        HumanMessage(content=str(trigger["message"])),
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "ok": False,
                                    "repairable": True,
                                    "error": (
                                        "record data failed schema validation at energy: "
                                        "'high' is not of type 'integer'"
                                    ),
                                }
                            ),
                            name="submit_job_record",
                            tool_call_id="call-bad",
                        ),
                        AIMessage(content="I could not store the record."),
                    ]
                }
                return job_result_from_graph_result(
                    graph_result,
                    job=job,
                    message=str(trigger["message"]),
                    trigger=trigger,
                )

            submitted = records.submit_record(
                thing_id=thing_id,
                source_job_id=job.id,
                source_run_id=run.id,
                data={"mood": "good", "energy": 4, "note": "slept well"},
                raw_input=str(trigger["message"]),
                confidence=0.91,
            )
            graph_result = {
                "messages": [
                    HumanMessage(content=str(trigger["message"])),
                    ToolMessage(
                        content=json.dumps(
                            {
                                "ok": True,
                                "record": submitted,
                            }
                        ),
                        name="submit_job_record",
                        tool_call_id="call-good",
                    ),
                    AIMessage(content="Recorded the corrected wellbeing check-in."),
                ]
            }
            return job_result_from_graph_result(
                graph_result,
                job=job,
                message=str(trigger["message"]),
                trigger=trigger,
            )

        async def close(self) -> None:
            return None

    publisher = _NoopJobRunPublisher()
    executor = JobExecutor(
        Settings(redis_url=jobs_integration_environment.redis_url),
        repo=repo,
        agent_runner=_RepairLoopAgentRunner(),
        event_publisher=publisher,
    )

    bad_reply = _run(
        executor.run_job(
            job.id,
            {
                "source": "user_reply",
                "message": "Mood good and energy high.",
                "client_reply_id": "repair-reply-1",
                "previous_run_id": run.id,
            },
        )
    )

    after_bad_job = _run(repo.get_job(job.id))
    after_bad_run = _run(repo.get_job_run(run.id))
    assert bad_reply["status"] == JobRunStatus.WAITING_FOR_INPUT.value
    assert "energy" in bad_reply["waiting_question"]
    assert after_bad_job.active_run_id == run.id
    assert after_bad_job.last_run_status == JobRunStatus.WAITING_FOR_INPUT
    assert after_bad_job.waiting_question == bad_reply["waiting_question"]
    assert after_bad_run.status == JobRunStatus.WAITING_FOR_INPUT
    assert records.read_property(thing_id, "record_count") == 0

    good_reply = _run(
        executor.run_job(
            job.id,
            {
                "source": "user_reply",
                "message": "Mood good, energy 4, note slept well.",
                "client_reply_id": "repair-reply-2",
                "previous_run_id": run.id,
            },
        )
    )

    completed_job = _run(repo.get_job(job.id))
    completed_run = _run(repo.get_job_run(run.id))
    events = _run(repo.list_job_run_events(job.id))

    assert good_reply["ok"] is True
    assert good_reply["submitted_record"]["data"]["energy"] == 4
    assert completed_job.active_run_id is None
    assert completed_job.last_run_status == JobRunStatus.SUCCEEDED
    assert completed_job.waiting_question is None
    assert completed_job.run_count == 1
    assert completed_run.status == JobRunStatus.SUCCEEDED
    assert records.read_property(thing_id, "record_count") == 1
    assert records.read_property(thing_id, "latest_energy") == 4
    assert publisher.published == [(job.id, run.id), (job.id, run.id)]

    event_types = [event.event_type for event in events]
    assert event_types == [
        JobRunEventType.RUN_STARTED,
        JobRunEventType.WAITING_FOR_INPUT,
        JobRunEventType.USER_REPLY,
        JobRunEventType.WAITING_FOR_INPUT,
        JobRunEventType.USER_REPLY,
        JobRunEventType.RECORD_SUBMITTED,
        JobRunEventType.ASSISTANT_MESSAGE,
        JobRunEventType.RUN_SUCCEEDED,
    ]
    assert "energy" in events[3].message
    assert events[4].message == "Mood good, energy 4, note slept well."
    assert events[5].payload["data"]["energy"] == 4
