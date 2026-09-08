"""LangChain tools for managing automation jobs via wotbot's in-process JobService.

These tools only run inside the API-process agent graph (see ``LOCAL_TOOLS``), where the
``JobService`` singleton is registered with ``set_active_job_service``. They therefore call
it directly instead of round-tripping through the HTTP job API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, ValidationError

from wotbot.jobs.active import get_active_job_service
from wotbot.jobs.enums import (
    JobActionKind,
    JobInteractionMode,
    JobOutputKind,
)
from wotbot.jobs.schemas import CreateJobRequest

if TYPE_CHECKING:
    from wotbot.jobs.service import JobService

_SERVICE_UNAVAILABLE = {"error": "Job service is not ready"}


def _thread_id_from_config(
    config: RunnableConfig,
    created_from_thread_id: str | None,
) -> str:
    return created_from_thread_id or config.get("configurable", {}).get("thread_id", "default")


def _trigger_payload(
    *,
    trigger_kind: str,
    schedule_kind: str | None,
    run_at: str | None,
    interval_seconds: int | None,
    cron_expression: str | None,
    cron_timezone: str | None,
    thing_id: str | None,
    event_name: str | None,
    subscription_input: Any,
) -> dict[str, Any]:
    if trigger_kind == "time":
        if schedule_kind is None:
            if cron_expression:
                schedule_kind = "cron"
            elif run_at:
                schedule_kind = "once"
            elif interval_seconds is not None:
                schedule_kind = "interval"
            else:
                raise ValueError("time jobs require run_at or interval_seconds")
        schedule: dict[str, Any] = {"kind": schedule_kind}
        if schedule_kind == "once":
            schedule["run_at"] = run_at
        elif schedule_kind == "interval":
            schedule["interval_seconds"] = interval_seconds
        elif schedule_kind == "cron":
            schedule["expression"] = cron_expression
            if cron_timezone:
                schedule["timezone"] = cron_timezone
        return {"kind": "time", "schedule": schedule}
    return {
        "kind": "event",
        "thing_id": thing_id,
        "event_name": event_name,
        "subscription_input": subscription_input,
    }


async def _run_job(service: JobService, job_id: str) -> dict[str, Any]:
    try:
        return await service.run_job_now(job_id)
    except KeyError:
        return {"ok": False, "error": "job not found"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _create_job(
    service: JobService,
    request: CreateJobRequest,
) -> dict[str, Any]:
    try:
        job = await service.create_job(request)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}

    return job.model_dump(mode="json", by_alias=True)


@tool
async def create_prompt_job(
    name: str,
    run_instructions: Annotated[
        str,
        Field(
            description=(
                "Instruction the background worker should follow each time the job "
                "runs. Convert the user's creation request into this runtime behavior; "
                "put timing in trigger fields."
            )
        ),
    ],
    trigger_kind: str,
    config: RunnableConfig,
    interaction_mode: str = JobInteractionMode.AUTONOMOUS.value,
    schedule_kind: str | None = None,
    created_from_thread_id: str | None = None,
    run_at: str | None = None,
    interval_seconds: int | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    thing_id: str | None = None,
    event_name: str | None = None,
    subscription_input: Any = None,
) -> dict[str, Any]:
    """Create a prompt automation job that runs natural-language instructions.

    run_instructions is the instruction the background worker will execute later.
    For example, if the user says "create a job to check the production queue every hour",
    pass run_instructions="Check the production queue."

    trigger_kind:
    - "time": use run_at (ISO datetime), interval_seconds, or cron_expression
    - "event": use thing_id and event_name

    schedule_kind:
    - "once": run one time at run_at
    - "interval": run every interval_seconds
    - "cron": run on cron_expression, with optional cron_timezone such as "Europe/Berlin"
    """
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    try:
        request = CreateJobRequest(
            name=name,
            created_from_thread_id=_thread_id_from_config(config, created_from_thread_id),
            interaction_mode=interaction_mode,
            action={"kind": "prompt", "prompt": run_instructions},
            trigger=_trigger_payload(
                trigger_kind=trigger_kind,
                schedule_kind=schedule_kind,
                run_at=run_at,
                interval_seconds=interval_seconds,
                cron_expression=cron_expression,
                cron_timezone=cron_timezone,
                thing_id=thing_id,
                event_name=event_name,
                subscription_input=subscription_input,
            ),
            output={"kind": "narrative"},
        )
    except (ValidationError, ValueError) as exc:
        return {"error": str(exc)}
    return await _create_job(service, request)


@tool
async def create_record_prompt_job(
    name: str,
    run_instructions: Annotated[
        str,
        Field(
            description=(
                "Instruction the background worker should follow each time the job "
                "runs. Describe what to ask or generate and when to submit_job_record; "
                "put timing in trigger fields."
            )
        ),
    ],
    record_schema: dict[str, Any],
    trigger_kind: str,
    config: RunnableConfig,
    interaction_mode: str = JobInteractionMode.REQUIRED_CHECKIN.value,
    virtual_thing_title: str | None = None,
    virtual_thing_description: str | None = None,
    virtual_thing_id: str | None = None,
    schedule_kind: str | None = None,
    created_from_thread_id: str | None = None,
    run_at: str | None = None,
    interval_seconds: int | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    thing_id: str | None = None,
    event_name: str | None = None,
    subscription_input: Any = None,
) -> dict[str, Any]:
    """Create a prompt job that stores user/model answers as queryable records.

    The backend validates the JSON Schema and generates a virtual Thing Description
    with latest-value properties and history query actions.
    run_instructions is the future run instruction, not the user's request to create
    this job.
    """
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    try:
        request = CreateJobRequest(
            name=name,
            created_from_thread_id=_thread_id_from_config(config, created_from_thread_id),
            interaction_mode=interaction_mode,
            action={"kind": "prompt", "prompt": run_instructions},
            output={
                "kind": JobOutputKind.STRUCTURED_RECORD.value,
                "schema": record_schema,
                "virtual_thing": {
                    "id": virtual_thing_id,
                    "title": virtual_thing_title,
                    "description": virtual_thing_description,
                },
            },
            trigger=_trigger_payload(
                trigger_kind=trigger_kind,
                schedule_kind=schedule_kind,
                run_at=run_at,
                interval_seconds=interval_seconds,
                cron_expression=cron_expression,
                cron_timezone=cron_timezone,
                thing_id=thing_id,
                event_name=event_name,
                subscription_input=subscription_input,
            ),
        )
    except (ValidationError, ValueError) as exc:
        return {"error": str(exc)}
    return await _create_job(service, request)


@tool
async def create_analysis_job(
    name: str,
    analysis_code: str,
    trigger_kind: str,
    config: RunnableConfig,
    schedule_kind: str | None = None,
    created_from_thread_id: str | None = None,
    run_at: str | None = None,
    interval_seconds: int | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    thing_id: str | None = None,
    event_name: str | None = None,
    subscription_input: Any = None,
    record_schema: dict[str, Any] | None = None,
    virtual_thing_title: str | None = None,
    virtual_thing_description: str | None = None,
    virtual_thing_id: str | None = None,
) -> dict[str, Any]:
    """Create an analysis job that runs Python in the code-executor sandbox.

    trigger_kind:
    - "time": use run_at, interval_seconds, or cron_expression (recurring calendar cadence)
    - "event": use thing_id and event_name to run on a subscribed WoT event

    schedule_kind:
    - "once": run one time at run_at
    - "interval": run every interval_seconds
    - "cron": run on cron_expression, with optional cron_timezone such as "Europe/Berlin"

    analysis_code should call report("...") with one plain-language sentence
    summarizing the result; that headline is what the user sees in toasts and
    notifications (print output stays in the run details).
    Event-triggered analysis code can read event_payload for the decoded event,
    event for metadata, and job_trigger/trigger_payload for raw trigger details.

    record_schema (optional): when given, the job produces a virtual Thing whose
    properties/history come from records the analysis code stores. The code calls
    store_record(data: dict, raw_input=None, confidence=None) for each record; data
    must satisfy this JSON Schema. Omit for plain narrative (report/chart) output.
    """
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    if record_schema is not None:
        output: dict[str, Any] = {
            "kind": JobOutputKind.STRUCTURED_RECORD.value,
            "schema": record_schema,
            "virtual_thing": {
                "id": virtual_thing_id,
                "title": virtual_thing_title,
                "description": virtual_thing_description,
            },
        }
    else:
        output = {"kind": JobOutputKind.NARRATIVE.value}
    try:
        request = CreateJobRequest(
            name=name,
            created_from_thread_id=_thread_id_from_config(config, created_from_thread_id),
            action={"kind": JobActionKind.ANALYSIS.value, "analysis_code": analysis_code},
            output=output,
            trigger=_trigger_payload(
                trigger_kind=trigger_kind,
                schedule_kind=schedule_kind,
                run_at=run_at,
                interval_seconds=interval_seconds,
                cron_expression=cron_expression,
                cron_timezone=cron_timezone,
                thing_id=thing_id,
                event_name=event_name,
                subscription_input=subscription_input,
            ),
        )
    except (ValidationError, ValueError) as exc:
        return {"error": str(exc)}
    return await _create_job(service, request)


@tool
async def list_jobs(created_from_thread_id: str | None = None) -> dict[str, Any]:
    """List automation jobs, optionally filtered by creating chat thread.

    If created_from_thread_id is omitted, returns all jobs.
    """
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    jobs = await service.list_jobs(created_from_thread_id=created_from_thread_id)
    return {"jobs": [job.model_dump(mode="json", by_alias=True) for job in jobs]}


@tool
async def delete_job(job_id: str) -> dict[str, Any]:
    """Delete an automation job by id."""
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    try:
        job = await service.delete_job(job_id)
    except KeyError:
        return {"error": "job not found"}
    except Exception as exc:
        return {"error": str(exc)}
    return {"ok": True, "job": job.model_dump(mode="json", by_alias=True)}


@tool
async def run_job_now(job_id: str) -> dict[str, Any]:
    """Trigger an automation job immediately and return the execution result."""
    service = get_active_job_service()
    if service is None:
        return dict(_SERVICE_UNAVAILABLE)
    return await _run_job(service, job_id)
