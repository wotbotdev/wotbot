"""Connection-independent agent execution over the shared graph lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import aclosing, suppress
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from wotbot.agent_api.errors import TaskNotCancelableError, UnsupportedOperationError
from wotbot.agent_api.interrupts import describe_interrupt
from wotbot.agent_api.store import ACTIVE, RESERVED, Admission, TaskStore
from wotbot.agent_api.types import (
    Invocation,
    Message,
    Part,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from wotbot.auth.providers import get_agent_principal
from wotbot.core.agent_runs import (
    RunRegistry,
    _finalize_interrupted_run,
    _message_text,
    stream_run_events,
)
from wotbot.core.time import utc_now
from wotbot.threads import sync_thread_after_run

logger = logging.getLogger(__name__)

# Handed to a listener that fell too far behind, instead of ending its
# stream the same way a finished task does.
_TRUNCATED = object()


def agent_message(task: Task, text: str, data: dict | None = None) -> Message:
    parts = [Part(text=text, media_type="text/plain")] if text else []
    if data is not None:
        parts.append(Part(data=data, media_type="application/json"))
    return Message(
        message_id=str(uuid4()),
        context_id=task.context_id,
        task_id=task.id,
        role="ROLE_AGENT",
        parts=parts,
    )


class AgentRuntime:
    def __init__(
        self,
        *,
        graph,
        registry: RunRegistry,
        settings,
        store=None,
        collector_factory=None,
        sync_thread=sync_thread_after_run,
        is_authorized=None,
        raw_graph=None,
        subscriptions=None,
    ):
        self.graph, self.registry, self.settings = graph, registry, settings
        self.raw_graph = raw_graph
        self.subscriptions = subscriptions
        self.store = store or TaskStore(retention_days=settings.a2a_task_retention_days)
        self.collector_factory = collector_factory
        self.sync_thread = sync_thread
        self.is_authorized = is_authorized or self._key_authorized
        self.runners: dict[str, asyncio.Task] = {}
        self.listeners: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.admission_lock = asyncio.Lock()
        self.event_locks = RunRegistry()
        self.closing = False
        self.revoked: set[str] = set()
        self.maintenance: asyncio.Task | None = None

    async def start(self):
        await asyncio.to_thread(self.store.acquire_execution_lease)
        try:
            recovered = await asyncio.to_thread(self.store.recover)
            await self.sweep()
        except BaseException:
            await asyncio.to_thread(self.store.release_execution_lease)
            raise
        logger.info("Agent started; failed %d abandoned tasks (no automatic retries)", recovered)
        self.maintenance = asyncio.create_task(self._maintain())

    async def _maintain(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.sweep()
            except Exception:
                logger.exception("Agent retention cleanup failed")

    async def sweep(self) -> None:
        """Expire tasks, then retire the conversations they were the last of.

        Checkpoints go before the thread rows: interrupted halfway, the next
        sweep sees the same threads again rather than leaving graph state
        behind with nothing pointing at it.
        """
        if self.subscriptions is not None:
            await self.subscriptions.sweep()
        # Admission must not attach new work after the retirement snapshot and
        # before checkpoint/thread deletion. This also serializes concurrent sweeps.
        async with self.admission_lock:
            retired = await asyncio.to_thread(self.store.prune)
            checkpointer = getattr(self.graph, "checkpointer", None)
            for thread_id in retired:
                async with self.registry.thread_lock(thread_id):
                    if checkpointer is not None:
                        await checkpointer.adelete_thread(thread_id)
            deleted = await asyncio.to_thread(self.store.delete_contexts, retired)
        if deleted:
            logger.info("Agent retention retired %d context(s)", deleted)

    async def close(self):
        # Include admissions shielded from a disconnected request before taking
        # the shutdown snapshot; no runner may start after graph teardown.
        async with self.admission_lock:
            self.closing = True
            runners = list(self.runners.values())
        if self.maintenance:
            self.maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await self.maintenance
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        await asyncio.to_thread(self.store.release_execution_lease)

    def _key_authorized(self, owner: str) -> bool:
        return get_agent_principal(owner, session_factory=self.store.session_factory) is not None

    async def admit(self, owner: str, params) -> Admission:
        Invocation.model_validate(params)
        request = params
        async with self.admission_lock:
            if self.closing:
                raise UnsupportedOperationError("Server is shutting down")
            admission = await asyncio.to_thread(self.store.admit, owner, request)
            if admission.execute:
                runner = asyncio.create_task(
                    self._run(owner, admission, request["message"]),
                    name=f"agent:{admission.task.id}",
                )
                self.runners[admission.task.id] = runner
                runner.add_done_callback(lambda done: self._runner_done(admission.task.id, done))
                logger.info(
                    "Admitted agent task=%s context=%s",
                    admission.task.id,
                    admission.task.context_id,
                )
            return admission

    def _runner_done(self, task_id, runner):
        if self.runners.get(task_id) is runner:
            self.runners.pop(task_id, None)
        self.revoked.discard(task_id)
        if not runner.cancelled() and runner.exception():
            logger.error("Agent runner failed task=%s", task_id, exc_info=runner.exception())

    async def _watch_key(self, owner, task_id, runner):
        while True:
            try:
                authorized = await asyncio.to_thread(self.is_authorized, owner)
            except Exception:
                logger.exception("Could not verify invoking key for A2A task=%s", task_id)
                authorized = False
            if not authorized:
                self.revoked.add(task_id)
                runner.cancel()
                return
            await asyncio.sleep(1)

    async def _status(self, owner, task, state, text="", data=None, pending=None, message=None):
        task.status.state = state
        task.status.timestamp = utc_now()
        if message is not None:
            task.status.message = message
        elif text or data:
            task.status.message = agent_message(task, text, data)
        else:
            task.status.message = None
        async with self.event_locks.thread_lock(task.id):
            await asyncio.to_thread(self.store.save, owner, task, pending)
            self._broadcast(
                task.id,
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=task.status.model_copy(deep=True),
                ),
            )
        logger.info(
            "Agent task=%s context=%s state=%s", task.id, task.context_id, TaskState.Name(state)
        )

    def _broadcast(self, task_id, event):
        for queue in tuple(self.listeners.get(task_id, ())):
            if queue.full():
                # A slow consumer must not hold up execution. Drop what it has
                # not read and say so, rather than ending its stream the way a
                # completed task would and letting it believe it saw everything.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(_TRUNCATED)
                self.listeners[task_id].discard(queue)
            else:
                queue.put_nowait(event)

    async def _run(self, owner: str, admission: Admission, message: dict):
        task, thread_id = admission.task, admission.thread_id
        operation = admission.operation
        graph = self.raw_graph if operation.family == "raw" else self.graph
        if graph is None:
            raise RuntimeError("Execution graph is unavailable")
        watcher = asyncio.create_task(self._watch_key(owner, task.id, asyncio.current_task()))
        try:
            await self._status(owner, task, TaskState.TASK_STATE_WORKING)
            config = {"configurable": {"thread_id": thread_id}}
            prior = await graph.aget_state(config)
            seen = {m.id for m in (prior.values or {}).get("messages", [])} if prior else set()
            if self.collector_factory is None:
                from wotbot.agent_api.outputs import ArtifactCollector

                collector = ArtifactCollector(
                    settings=self.settings, owner=owner, task_id=task.id, thread_id=thread_id
                )
            else:
                collector = self.collector_factory(
                    owner=owner, task_id=task.id, thread_id=thread_id
                )
            text = "\n".join(
                p.get("text", json.dumps(p.get("data"), ensure_ascii=False))
                for p in message["parts"]
            )
            events = stream_run_events(
                graph=graph,
                registry=self.registry,
                thread_id=thread_id,
                input_data={"messages": [HumanMessage(content=text, id=message["messageId"])]},
                context={
                    "external_agent": True,
                    "owner": owner,
                    "task_id": task.id,
                    "operation": operation.json(),
                    "resume_responses": admission.resume,
                    "forced_intent": operation.name if operation.kind == "intent" else None,
                },
                command={"resume": admission.resume} if admission.resume is not None else None,
                sync_thread=self.sync_thread,
            )
            async with aclosing(events):
                async for event in events:
                    if event.mode == "error":
                        raise event.payload
                    for artifact in await collector.consume(event, seen=seen):
                        async with self.event_locks.thread_lock(task.id):
                            task.artifacts.append(artifact)
                            await asyncio.to_thread(self.store.save, owner, task)
                            self._broadcast(
                                task.id,
                                TaskArtifactUpdateEvent(
                                    task_id=task.id,
                                    context_id=task.context_id,
                                    artifact=artifact,
                                    last_chunk=True,
                                ),
                            )
            snapshot = await graph.aget_state(config, subgraphs=True)
            failures = list(task.metadata.get("wotbotOutputErrors", []))
            failures = list(dict.fromkeys([*failures, *collector.failures]))
            if failures:
                # A paused task may resume in another process. Keep export errors
                # with it so a continuation cannot silently report full success.
                task.metadata["wotbotOutputErrors"] = failures
            interruptions = list(getattr(snapshot, "interrupts", ()) or ())
            if not interruptions:
                interruptions = [i for t in (snapshot.tasks or ()) for i in t.interrupts]
            if interruptions:
                pending = [describe_interrupt(i) for i in interruptions]
                state = (
                    TaskState.TASK_STATE_AUTH_REQUIRED
                    if any(p["kind"] == "credential" for p in pending)
                    else TaskState.TASK_STATE_INPUT_REQUIRED
                )
                await self._status(
                    owner,
                    task,
                    state,
                    "\n".join(p["explanation"] for p in pending)
                    + ("\nOutput export failures: " + "; ".join(failures) if failures else ""),
                    {"kind": "wotbot.input_requests", "requests": pending},
                    pending=pending,
                )
            else:
                messages = (snapshot.values or {}).get("messages", [])
                reply = next(
                    (
                        _message_text(m.content)
                        for m in reversed(messages)
                        if isinstance(m, AIMessage) and not m.tool_calls and m.id not in seen
                    ),
                    "",
                )
                output = agent_message(task, reply) if reply else None
                if output is not None:
                    task.history.append(output)
                if operation.family == "raw":
                    task.metadata["wotbotResult"] = snapshot.values.get("raw_result")
                raw_error = snapshot.values.get("raw_error") if operation.family == "raw" else None
                if raw_error:
                    await self._status(owner, task, TaskState.TASK_STATE_FAILED, raw_error)
                elif failures:
                    await self._status(
                        owner,
                        task,
                        TaskState.TASK_STATE_FAILED,
                        "Some generated outputs could not be exported. " + "; ".join(failures),
                    )
                else:
                    await self._status(owner, task, TaskState.TASK_STATE_COMPLETED, message=output)
        except asyncio.CancelledError:
            if self.closing or task.id in self.revoked:
                await self._status(
                    owner,
                    task,
                    TaskState.TASK_STATE_FAILED,
                    "Execution stopped because the server is shutting down or the invoking API key was revoked, expired, or could no longer be verified. Device action outcomes may be uncertain; work was not retried.",
                )
            else:
                await self._status(
                    owner,
                    task,
                    TaskState.TASK_STATE_CANCELED,
                    "Execution was cancelled and checkpoint cleanup finished. Completed device actions are not undone.",
                )
        except Exception:
            logger.exception("Agent execution failed task=%s context=%s", task.id, task.context_id)
            await self._status(
                owner,
                task,
                TaskState.TASK_STATE_FAILED,
                "Execution failed. Device actions may already have occurred; inspect current state before retrying.",
            )
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    async def subscribe(
        self, owner: str, task_id: str, *, terminal_error=False, history_length=None, family=None
    ):
        queue = asyncio.Queue(maxsize=256)
        try:
            # Atomically take the durable snapshot and join future broadcasts.
            # The family check reads the same row under the same lock: an await
            # ahead of it would let a fast runner reach a terminal state before
            # this listener joined, leaving the stream with no updates at all.
            async with self.event_locks.thread_lock(task_id):
                task = await asyncio.to_thread(self.store.get, owner, task_id, family)
                if terminal_error and TaskState.Name(task.status.state) not in RESERVED:
                    raise UnsupportedOperationError("Use GetTask to retrieve a terminal task")
                self.listeners[task_id].add(queue)
            if history_length is not None:
                history = list(task.history)[-history_length:] if history_length else []
                del task.history[:]
                task.history.extend(history)
            yield task
            if TaskState.Name(task.status.state) not in ACTIVE:
                return
            while True:
                event = await queue.get()
                if event is _TRUNCATED:
                    logger.warning(
                        "Agent subscriber fell behind; truncating stream task=%s", task_id
                    )
                    current = await asyncio.to_thread(self.store.get, owner, task_id)
                    notice = TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id=current.context_id,
                        status=current.status,
                    )
                    notice.metadata["wotbotStreamTruncated"] = True
                    yield notice
                    return
                yield event
                if (
                    isinstance(event, TaskStatusUpdateEvent)
                    and TaskState.Name(event.status.state) not in ACTIVE
                ):
                    return
        finally:
            self.listeners[task_id].discard(queue)
            if not self.listeners[task_id]:
                self.listeners.pop(task_id, None)

    async def cancel(self, owner: str, task_id: str, *, family=None) -> Task:
        async with self.admission_lock:
            task = await asyncio.to_thread(self.store.get, owner, task_id, family)
            if TaskState.Name(task.status.state) not in RESERVED:
                raise TaskNotCancelableError()
            runner = self.runners.get(task_id)
            stopped_runner = bool(runner and not runner.done())
            if stopped_runner:
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner
            thread_id = await asyncio.to_thread(self.store.thread_id, owner, task_id)
            # Also finalizes a deliberately suspended task, which has no active runner.
            await self.registry.cancel_and_wait(thread_id)
            async with self.registry.thread_lock(thread_id):
                operation = await asyncio.to_thread(self.store.operation, owner, task_id)
                graph = self.raw_graph if operation.family == "raw" else self.graph
                await _finalize_interrupted_run(graph, thread_id)
            task = await asyncio.to_thread(self.store.get, owner, task_id, family)
            if stopped_runner and TaskState.Name(task.status.state) not in RESERVED:
                # The runner's own cancellation path already recorded why it
                # stopped; a second terminal transition would only overwrite it.
                return task
            await self._status(
                owner,
                task,
                TaskState.TASK_STATE_CANCELED,
                "Execution was cancelled and checkpoint cleanup finished. Completed actions are not undone.",
            )
            return task

    async def wait(self, owner, task_id, seconds=0, *, family=None):
        if seconds:
            try:
                async with asyncio.timeout(seconds):
                    async with aclosing(self.subscribe(owner, task_id, family=family)) as events:
                        async for _ in events:
                            pass
            except TimeoutError:
                pass
        return await asyncio.to_thread(self.store.get, owner, task_id, family)
