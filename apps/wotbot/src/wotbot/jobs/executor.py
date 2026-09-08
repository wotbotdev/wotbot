from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
except ImportError:  # pragma: no cover - dependency is installed in the app image.
    AsyncPostgresSaver = None  # type: ignore[assignment]

from wotbot.agent import build_background_job_graph
from wotbot.agent.tools.ask_job_user import ask_job_user
from wotbot.agent.tools.get_current_time import get_current_time
from wotbot.agent.tools.run_code import run_code
from wotbot.agent.tools.submit_job_record import submit_job_record
from wotbot.agent.tools.wot_registry import REGISTRY_TOOLS
from wotbot.clients.code_executor import (
    CodeExecutorClient,
    format_code_execution_result,
)
from wotbot.core.config import get_settings as get_registry_settings
from wotbot.core.database import init_db, psycopg_conninfo
from wotbot.core.llm import make_llm
from wotbot.core.settings import Settings
from wotbot.jobs.enums import JobRunSource, JobRunStatus
from wotbot.jobs.graph_results import (
    graph_config_for_run,
    graph_input_for_run,
    job_result_from_graph_result,
    job_run_status_from_result,
)
from wotbot.jobs.record_summary import submitted_record_summary
from wotbot.jobs.records import VirtualRecordStore
from wotbot.jobs.results import JobRunEventPublisher
from wotbot.jobs.schemas import AnalysisAction, Job, JobRun, StructuredRecordOutput
from wotbot.jobs.stores import JobStore, utc_now
from wotbot.search import ThingSearchService, set_active_search_service

logger = logging.getLogger(__name__)


class BackgroundAgentRunner:
    """Lazy LangGraph runtime used by prompt jobs running outside a chat request."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._graph: Any | None = None
        self._checkpointer_context: Any | None = None
        self._checkpointer: Any | None = None
        self._search_service: ThingSearchService | None = None

    async def close(self) -> None:
        set_active_search_service(None)
        if self._checkpointer_context is not None:
            await self._checkpointer_context.__aexit__(None, None, None)
            self._checkpointer_context = None
            self._checkpointer = None
        if self._search_service is not None:
            await self._search_service.close()
            self._search_service = None
        self._graph = None

    async def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph

        if AsyncPostgresSaver is None:
            raise RuntimeError(
                "Postgres checkpointing requires langgraph-checkpoint-postgres to be installed"
            )

        init_db()
        registry_settings = get_registry_settings()
        self._search_service = ThingSearchService(registry_settings)
        set_active_search_service(self._search_service)
        database_url = self._settings.agent_state_database_url or registry_settings.DATABASE_URL
        self._checkpointer_context = AsyncPostgresSaver.from_conn_string(
            psycopg_conninfo(database_url)
        )
        self._checkpointer = await self._checkpointer_context.__aenter__()
        await self._checkpointer.setup()

        llm = make_llm(self._settings)
        graph = build_background_job_graph(
            llm=llm,
            registry_tools=REGISTRY_TOOLS,
            local_tools=[
                run_code,
                get_current_time,
                ask_job_user,
                submit_job_record,
            ],
            max_tokens=self._settings.max_context_tokens,
            checkpointer=self._checkpointer,
            parallel_tool_calls=self._settings.parallel_tool_calls,
        )
        self._graph = graph
        return self._graph

    async def run(
        self,
        job: Job,
        *,
        run: JobRun,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:
        graph = await self._ensure_graph()
        message = trigger.get("message") if trigger.get("source") == "user_reply" else None
        graph_input = graph_input_for_run(job, run, message)
        graph_config = graph_config_for_run(job, run, self._settings.recursion_limit)
        result = await graph.ainvoke(graph_input, config=graph_config)
        return job_result_from_graph_result(result, job=job, message=message, trigger=trigger)


class JobExecutor:
    """Runs queued job tasks and records their durable run state."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo: JobStore | None = None,
        code_executor_client: CodeExecutorClient | None = None,
        agent_runner: BackgroundAgentRunner | None = None,
        event_publisher: JobRunEventPublisher | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo or JobStore()
        self._code_executor_client = code_executor_client or CodeExecutorClient(settings)
        self._agent_runner = agent_runner or BackgroundAgentRunner(settings)
        self._event_publisher = event_publisher or JobRunEventPublisher(settings, repo=self._repo)

    async def close(self) -> None:
        await self._agent_runner.close()

    async def reconcile_stale_running_runs(self) -> int:
        stale_after_seconds = getattr(
            self._settings,
            "job_run_stale_after_seconds",
            max(self._settings.job_task_timeout_seconds * 2, 600),
        )
        cutoff = utc_now() - timedelta(seconds=stale_after_seconds)
        stale_count = await self._repo.mark_stale_running_runs_failed(cutoff=cutoff)
        if stale_count:
            logger.warning("Marked %d stale job run(s) failed on worker startup", stale_count)
        return stale_count

    async def run_job(self, job_id: str, trigger: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        if trigger.get("source") == "user_reply":
            run = await self._repo.start_reply_job_run(
                job_id=job_id,
                message=str(trigger.get("message") or ""),
                client_reply_id=trigger.get("client_reply_id"),
                previous_run_id=trigger.get("previous_run_id"),
                now=now,
            )
            run_source = run.source
        else:
            run_source = _run_source_from_trigger(trigger)
            run = await self._repo.try_start_job_run(
                job_id=job_id,
                source=run_source,
                trigger_payload=trigger,
                now=now,
            )

        if run is None:
            return {"ok": False, "error": "Job run was not started."}

        if run.status == JobRunStatus.SKIPPED:
            await self._event_publisher.publish_job_run(job_id, run_id=run.id)
            return {
                "ok": False,
                "status": JobRunStatus.SKIPPED.value,
                "job_id": job_id,
                "run_id": run.id,
                "error": run.error or "Job run skipped.",
                "assistant": run.response_text,
            }

        if _is_duplicate_reply_run(run):
            await self._event_publisher.publish_job_run(job_id, run_id=run.id)
            return _duplicate_reply_result(run)

        job = await self._repo.get_job(job_id)

        if isinstance(job.action, AnalysisAction):
            result = await self._run_analysis_job(job, run=run, trigger=trigger)
        else:
            result = await self._run_prompt_job(job, run=run, trigger=trigger)

        now = utc_now()
        is_scheduled_time_run = run_source == JobRunSource.TIME
        next_run_at = (
            _next_run_at_after_scheduled_time_run(job, now=now) if is_scheduled_time_run else None
        )
        status = job_run_status_from_result(result)

        await self._repo.finish_job_run(
            run_id=run.id,
            job_id=job.id,
            now=now,
            status=status,
            error=result.get("error"),
            response_text=result.get("assistant"),
            result=result,
            next_run_at=next_run_at,
            waiting_question=result.get("waiting_question"),
        )
        # A one-shot time job has fired its only run; disable it so startup
        # reconciliation does not re-create a schedule for it. The Redis schedule
        # itself is removed automatically by the source's post_send.
        if is_scheduled_time_run and job.is_one_shot_time_job():
            await self._repo.disable_job(job.id)
        await self._event_publisher.publish_job_run(job.id, run_id=run.id)
        return result

    async def _run_prompt_job(
        self,
        job: Job,
        *,
        run: JobRun,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self._agent_runner.run(job, run=run, trigger=trigger)
        except Exception as exc:
            logger.error("Failed prompt job %s: %s", job.id, exc, exc_info=exc)
            return {"ok": False, "error": str(exc), "metadata": {"trigger": trigger}}

    async def _run_analysis_job(
        self,
        job: Job,
        *,
        run: JobRun,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self._code_executor_client.execute(
                session_id=f"job-analysis:{job.id}",
                code=_analysis_code_for_run(job.action.analysis_code, trigger=trigger),
            )
            formatted = format_code_execution_result(response)
            if response.get("ok") is False:
                error = str(response.get("error") or "Code execution failed")
                return {
                    **formatted,
                    "ok": False,
                    "error": error,
                    "response": response,
                    "assistant": error[:4000],
                    "metadata": {"trigger": trigger},
                }
            stored_records = await self._store_analysis_records(job, run=run, response=response)
            stdout = str(formatted.get("stdout", "")).strip()
            artifacts = formatted.get("artifacts", [])
            if not isinstance(artifacts, list):
                artifacts = []

            assistant = _analysis_assistant(
                report=_joined_report(response),
                stdout=stdout,
                artifacts=artifacts,
                records=stored_records,
            )

            return {
                "ok": True,
                "response": response,
                **formatted,
                "records": stored_records,
                "assistant": assistant[:4000],
                "metadata": {"trigger": trigger},
            }
        except Exception as exc:
            logger.error("Failed analysis job %s: %s", job.id, exc, exc_info=exc)
            return {"ok": False, "error": str(exc), "metadata": {"trigger": trigger}}

    async def _store_analysis_records(
        self,
        job: Job,
        *,
        run: JobRun,
        response: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate and persist a record emitted via the sandbox ``store_record`` helper.

        Reuses the same ``VirtualRecordStore.submit_record`` validation/idempotency the
        prompt path uses. A run stores at most one record (the virtual-record model keys
        on one record per run); a schema-rejected record raises, failing the run with a
        clear error so the analysis code can be fixed (deterministic code cannot
        self-correct).
        """
        output = job.output
        if not isinstance(output, StructuredRecordOutput):
            return []
        raw_records = response.get("records")
        if not isinstance(raw_records, list) or not raw_records:
            return []
        if len(raw_records) > 1:
            raise ValueError(
                "store_record may be called at most once per analysis run; "
                f"it was called {len(raw_records)} times"
            )
        thing_id = output.virtual_thing_id
        if not thing_id:
            logger.warning("Analysis job %s has structured output but no virtual Thing id.", job.id)
            return []

        entry = raw_records[0]
        stored = await asyncio.to_thread(
            VirtualRecordStore().submit_record,
            thing_id=thing_id,
            source_job_id=job.id,
            source_run_id=run.id,
            data=entry.get("data"),
            raw_input=entry.get("raw_input"),
            confidence=entry.get("confidence"),
        )
        return [stored]


def _analysis_code_for_run(code: str, *, trigger: dict[str, Any]) -> str:
    """Inject job run context into deterministic analysis code.

    Analysis jobs are persisted as plain user-authored snippets. At execution time we
    add a tiny prelude so event jobs can read the triggering event without each agent
    inventing its own transport decoding.
    """
    trigger_json = json.dumps(trigger, ensure_ascii=True, default=str)
    return f"""\
import base64 as __job_base64
import json as __job_json

job_trigger = __job_json.loads({trigger_json!r})
trigger_payload = job_trigger

def __job_decode_event_payload(trigger):
    payload_base64 = trigger.get("payload_base64")
    if not isinstance(payload_base64, str) or not payload_base64:
        return None
    raw = __job_base64.b64decode(payload_base64)
    content_type = str(trigger.get("content_type") or "").lower()
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type:
        return __job_json.loads(text)
    try:
        return __job_json.loads(text)
    except Exception:
        return text

event_payload = __job_decode_event_payload(job_trigger)
event = {{
    "thing_id": job_trigger.get("thing_id"),
    "event_name": job_trigger.get("event_name"),
    "payload": event_payload,
    "content_type": job_trigger.get("content_type"),
    "timestamp": job_trigger.get("timestamp"),
}}
input = event_payload

{code}
"""


def _joined_report(response: dict[str, Any]) -> str:
    """Join the human-facing headlines emitted via the sandbox ``report`` helper."""
    raw = response.get("reports")
    if not isinstance(raw, list):
        return ""
    messages = [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]
    return "\n".join(messages)


def _analysis_assistant(
    *,
    report: str,
    stdout: str,
    artifacts: list[Any],
    records: list[Any],
) -> str:
    """Pick the headline for an analysis run, preferring authored over technical output.

    Order: an explicit ``report`` headline wins; otherwise raw ``stdout`` (with a
    note of what was produced); otherwise a summary of artifacts/records plus a
    generic "finished" so a chart-only run still reads cleanly.
    """
    if report:
        return report

    produced = _produced_summary(artifacts, records)
    if stdout and stdout != "(no output)":
        return "\n".join(part for part in (stdout, produced) if part)
    if produced:
        return f"{produced} · Analysis finished"
    return "Analysis finished"


def _produced_summary(artifacts: list[Any], records: list[Any]) -> str:
    """One-line summary of what a run produced: charts, images, and record content."""
    parts: list[str] = []
    artifact_summary = _artifact_summary(artifacts)
    if artifact_summary:
        parts.append(artifact_summary)
    if records:
        # One record per run; surface its field values, falling back to a count.
        parts.append(submitted_record_summary(records[0]) or "1 record")
    return ", ".join(parts)


def _artifact_summary(artifacts: list[Any]) -> str:
    images = 0
    charts = 0
    files = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("kind") == "image":
            images += 1
        elif artifact.get("kind") == "plotly":
            charts += 1
        elif artifact.get("kind") == "file":
            files += 1

    parts: list[str] = []
    if charts:
        parts.append(f"{charts} chart{'s' if charts != 1 else ''}")
    if images:
        parts.append(f"{images} image{'s' if images != 1 else ''}")
    if files:
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    return ", ".join(parts)


def _next_run_at_after_scheduled_time_run(job: Job, *, now: datetime) -> datetime | None:
    try:
        return job.next_run_at_after(now=now, enabled=job.enabled)
    except ValueError:
        return None


def _is_duplicate_reply_run(run: JobRun) -> bool:
    return (
        isinstance(run.trigger_payload, dict)
        and run.trigger_payload.get("_duplicate_reply") is True
    )


def _duplicate_reply_result(run: JobRun) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "duplicate_reply",
        "run": run.model_dump(mode="json"),
        "result": run.result if isinstance(run.result, dict) else None,
        "assistant": run.response_text,
    }


def _run_source_from_trigger(trigger: dict[str, Any]) -> JobRunSource:
    source = trigger.get("source")
    if source == "time":
        return JobRunSource.TIME
    if source in {"event", "wot_event"}:
        return JobRunSource.EVENT
    return JobRunSource.MANUAL


_executor: JobExecutor | None = None


def get_job_executor() -> JobExecutor:
    global _executor
    if _executor is None:
        settings = Settings()
        _executor = JobExecutor(settings)
    return _executor


async def close_job_executor() -> None:
    global _executor
    if _executor is not None:
        await _executor.close()
        _executor = None
