from __future__ import annotations

import logging
from datetime import UTC, datetime

from taskiq import ScheduledTask
from taskiq.abc.schedule_source import ScheduleSource
from taskiq_redis import ListRedisScheduleSource

from wotbot.core.settings import Settings
from wotbot.jobs.constants import JOB_SCHEDULE_PREFIX, RUN_JOB_TASK_NAME
from wotbot.jobs.schemas import Job, TimeTrigger
from wotbot.jobs.stores import JobStore
from wotbot.jobs.time_schedule import CronSchedule, IntervalSchedule, OnceSchedule

logger = logging.getLogger(__name__)


def build_schedule_source(settings: Settings) -> ScheduleSource:
    """Create the Redis-backed schedule store shared by the API, scheduler and worker.

    ``skip_past_schedules`` keeps one-shot ``run_at`` jobs from being replayed when the
    scheduler restarts after their time has already passed.
    """
    return ListRedisScheduleSource(
        settings.redis_url,
        prefix=JOB_SCHEDULE_PREFIX,
        skip_past_schedules=True,
    )


def schedule_id_for_job(job_id: str) -> str:
    return f"job:{job_id}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def scheduled_task_for_job(job: Job) -> ScheduledTask:
    """Build the taskiq schedule for a time-triggered job.

    Recurring jobs use native ``interval`` or ``cron`` schedules; one-shot jobs use
    ``time`` and are cleaned up automatically by the source's ``post_send`` once they
    fire.
    """
    common = {
        "task_name": RUN_JOB_TASK_NAME,
        "labels": {"job_id": job.id},
        "args": [],
        "kwargs": {"job_id": job.id, "trigger": {"source": "time"}},
        "schedule_id": schedule_id_for_job(job.id),
    }
    if not isinstance(job.trigger, TimeTrigger):
        raise ValueError(f"Job {job.id} is not time-triggered")
    schedule = job.trigger.schedule
    if isinstance(schedule, IntervalSchedule):
        return ScheduledTask(interval=schedule.interval_seconds, **common)
    if isinstance(schedule, OnceSchedule):
        return ScheduledTask(time=_utc(schedule.run_at), **common)
    if isinstance(schedule, CronSchedule):
        return ScheduledTask(
            cron=schedule.expression,
            cron_offset=schedule.timezone,
            **common,
        )
    raise ValueError(f"Time job {job.id} has an invalid schedule")


class JobScheduleManager:
    """Keeps the taskiq Redis schedule store in sync with time-triggered jobs.

    The job database is the source of truth. Schedules are written to Redis only when a
    job is created or deleted, plus a one-time reconciliation at startup -- never on a
    timer. Recurrence is handled natively by taskiq's ``interval`` and ``cron``
    schedules.
    """

    def __init__(
        self,
        source: ScheduleSource,
        *,
        repo: JobStore | None = None,
    ) -> None:
        self._source = source
        self._repo = repo or JobStore()

    async def add_job(self, job: Job) -> None:
        if not isinstance(job.trigger, TimeTrigger):
            return
        await self._source.add_schedule(scheduled_task_for_job(job))

    async def remove_job(self, job_id: str) -> None:
        await self._source.delete_schedule(schedule_id_for_job(job_id))

    async def sync(self) -> None:
        """Reconcile Redis schedules with the enabled time jobs in the database."""
        jobs = await self._repo.list_enabled_time_jobs()
        desired = {schedule_id_for_job(job.id): job for job in jobs}
        existing = {task.schedule_id for task in await self._source.get_schedules()}

        for schedule_id, job in desired.items():
            if schedule_id not in existing:
                await self._source.add_schedule(scheduled_task_for_job(job))

        for schedule_id in existing - desired.keys():
            await self._source.delete_schedule(schedule_id)

        logger.info("Synced %d time job schedule(s) to Redis.", len(desired))
