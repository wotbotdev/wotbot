"""Transport-independent graph execution, locking, and checkpoint finalization."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command

from wotbot.threads import suggest_thread_title, sync_thread_after_run

logger = logging.getLogger(__name__)

# "custom" is included so graph nodes can push progress via get_stream_writer()
# without another transport. "checkpoints"/"tasks"/"debug" are deliberately left
# out: the client has callbacks for them but nothing here consumes them, and
# they dominate payload size on graphs with large state.
RUN_STREAM_MODES: tuple[str, ...] = ("values", "messages", "updates", "custom")
_INTERRUPTED_TURN_NOTICE = "The response was interrupted before it finished. Try asking again."
# Stands in for a tool result that never arrived. Without it the call has no
# result forever, and the UI renders it as still executing on every reload.
_STOPPED_TOOL_RESULT = json.dumps(
    {"status": "stopped", "reason": "run_interrupted"}, separators=(",", ":")
)
_CONCURRENT_RUN_NOTICE = "A response is already running for this thread."


@dataclass(frozen=True, slots=True)
class RunEvent:
    """An application event before any transport serialization."""

    mode: str
    payload: Any
    namespace: tuple[str, ...] = ()


def _build_command(raw: Any) -> Command | None:
    if not isinstance(raw, dict):
        return None
    kwargs: dict[str, Any] = {}
    for key in ("resume", "goto", "update"):
        if raw.get(key) is not None:
            kwargs[key] = raw[key]
    return Command(**kwargs) if kwargs else None


def _is_interrupt_resume(raw: Any) -> bool:
    """Return whether this request is resuming a deliberate graph interrupt."""
    return isinstance(raw, dict) and raw.get("resume") is not None


@dataclass(slots=True)
class _ThreadLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class RunRegistry:
    """Tracks the in-flight run per thread so a cancel request can stop it."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._thread_locks: dict[str, _ThreadLockEntry] = {}

    def register(self, thread_id: str, task: asyncio.Task[None]) -> bool:
        existing = self._tasks.get(thread_id)
        if existing is not None and not existing.done():
            return False
        self._tasks[thread_id] = task
        return True

    def unregister(self, thread_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(thread_id) is task:
            del self._tasks[thread_id]

    def cancel(self, thread_id: str) -> bool:
        task = self._tasks.get(thread_id)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("Cancelled in-flight run thread_id=%s", thread_id)
        return True

    def is_running(self, thread_id: str) -> bool:
        task = self._tasks.get(thread_id)
        return task is not None and not task.done()

    async def cancel_and_wait(self, thread_id: str) -> bool:
        """Cancel a run and wait for its checkpoint finalizer to finish."""
        task = self._tasks.get(thread_id)
        if task is None or task.done():
            return False

        task.cancel()
        logger.info("Cancelled in-flight run before thread mutation thread_id=%s", thread_id)
        with suppress(asyncio.CancelledError):
            await task
        return True

    @asynccontextmanager
    async def thread_lock(self, thread_id: str) -> AsyncIterator[None]:
        entry = self._thread_locks.get(thread_id)
        if entry is None:
            entry = _ThreadLockEntry()
            self._thread_locks[thread_id] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._thread_locks.get(thread_id) is entry:
                del self._thread_locks[thread_id]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


class _PartialAssistantText:
    """Keep only the assistant message currently streaming when a run stops."""

    def __init__(self) -> None:
        self._message_id: str | None = None
        self._parts: list[str] = []

    def observe(self, mode: str, payload: Any) -> None:
        if mode != "messages" or not isinstance(payload, (list, tuple)) or not payload:
            return

        message = payload[0]
        if not isinstance(message, AIMessageChunk):
            return

        message_id = message.id if isinstance(message.id, str) else None
        if message_id and message_id != self._message_id:
            self._message_id = message_id
            self._parts.clear()

        text = _message_text(message.content)
        if text:
            self._parts.append(text)

        if message.chunk_position == "last":
            self._message_id = None
            self._parts.clear()

    def text(self) -> str:
        return "".join(self._parts).strip()


def _interrupted_turn_updates(
    messages: Sequence[Any],
    *,
    partial_text: str = "",
) -> list[BaseMessage]:
    """Messages that close the last user turn, or ``[]`` if it is already closed.

    A cancelled run can leave the checkpoint in more than one unfinished shape,
    because LangGraph checkpoints after every node:

    * ``[... Human]`` -- stopped before the model answered.
    * ``[... Human, AI(tool_calls)]`` -- stopped between the model and the tool
      node, so the calls have no results.
    * ``[... Human, AI(tool_calls), Tool]`` -- tools ran, but the final answer
      never arrived.

    Only the first was handled before, so the other two survived a reload as a
    turn that never ends -- and an unanswered call renders as permanently
    executing, since the UI derives that status from a missing result.

    Pairing the unanswered calls is for display only; the prompt is already
    protected by ``_sanitize_message_sequence`` in ``agent/nodes.py``.
    """
    last_human = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], HumanMessage)
        ),
        None,
    )
    if last_human is None:
        return []

    turn = messages[last_human + 1 :]
    # An assistant message with no tool calls ends the turn. Checking this
    # rather than the last message alone keeps the repair idempotent, so the
    # proactive and reactive callers can race without doubling up.
    if any(isinstance(message, AIMessage) and not message.tool_calls for message in turn):
        return []

    resolved = {message.tool_call_id for message in turn if isinstance(message, ToolMessage)}
    updates: list[BaseMessage] = []
    for message in turn:
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.tool_calls or ():
            tool_call_id = tool_call.get("id")
            if not isinstance(tool_call_id, str) or tool_call_id in resolved:
                continue
            resolved.add(tool_call_id)
            updates.append(ToolMessage(content=_STOPPED_TOOL_RESULT, tool_call_id=tool_call_id))

    updates.append(AIMessage(content=partial_text.strip() or _INTERRUPTED_TURN_NOTICE))
    return updates


async def _finalize_interrupted_run(
    graph: Any,
    thread_id: str,
    *,
    partial_text: str = "",
) -> None:
    """Close a checkpoint whose last user turn never received an answer."""
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    messages = state.values.get("messages", []) if state and state.values else []
    updates = _interrupted_turn_updates(messages, partial_text=partial_text)
    if not updates:
        return

    await graph.aupdate_state(config, {"messages": updates})


async def _sync_thread_metadata(
    graph: Any,
    thread_id: str,
    sync_thread: Callable[..., Any],
) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    messages = state.values.get("messages", []) if state and state.values else []
    title = suggest_thread_title(messages) if isinstance(messages, list) else None
    await asyncio.to_thread(sync_thread, thread_id, suggested_title=title)


async def _persist(operation: Any, *, error_message: str) -> None:
    """Shield checkpoint repair and metadata updates from request cancellation."""
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated cancel requests must still wait for the finalizer.
                continue
        task.result()
    except Exception:
        logger.exception(error_message)


async def _finish_run(
    *,
    graph: Any,
    registry: RunRegistry,
    thread_id: str,
    completed: bool,
    partial_text: str,
    sync_thread: Callable[..., Any],
) -> None:
    # Re-acquire the same lock used by graph execution. If a reconnect wins the
    # race, its proactive repair handles the dangling turn and this becomes a
    # harmless no-op after that run finishes.
    async with registry.thread_lock(thread_id):
        if not completed:
            await _finalize_interrupted_run(
                graph,
                thread_id,
                partial_text=partial_text,
            )
        await _sync_thread_metadata(graph, thread_id, sync_thread)


async def stream_run_events(
    *,
    graph: Any,
    registry: RunRegistry,
    thread_id: str,
    input_data: Any,
    context: dict[str, Any] | None,
    command: Any,
    sync_thread: Callable[..., Any] = sync_thread_after_run,
) -> AsyncIterator[RunEvent]:
    """Yield typed events for one graph run, cancellable via ``registry``.

    The graph is consumed by a background task feeding a queue rather than
    inline, so a cancel interrupts whatever the run is awaiting -- including a
    long LLM call or tool -- instead of taking effect only at the next stream
    part.
    """
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if context:
        configurable.update(context)
    config = {"configurable": configurable}

    graph_input = _build_command(command) or input_data
    resumes_interrupt = _is_interrupt_resume(command)
    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    partial = _PartialAssistantText()

    async def pump() -> None:
        completed = False
        run_error: Exception | None = None
        try:
            async with registry.thread_lock(thread_id):
                # Heal a prior cancellation that may still be finalizing in a
                # background task before this run appends another user turn.
                # A LangGraph interrupt intentionally leaves the turn open;
                # repairing it here would replace the pending tool result and
                # make the resume command a no-op.
                if not resumes_interrupt:
                    await _finalize_interrupted_run(graph, thread_id)
                async for namespace, mode, payload in graph.astream(
                    graph_input,
                    config=config,
                    stream_mode=list(RUN_STREAM_MODES),
                    subgraphs=True,
                ):
                    partial.observe(mode, payload)
                    await queue.put(RunEvent(mode, payload, tuple(namespace or ())))
            completed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Graph run failed thread_id=%s: %s", thread_id, exc)
            run_error = exc
        finally:
            await _persist(
                _finish_run(
                    graph=graph,
                    registry=registry,
                    thread_id=thread_id,
                    completed=completed,
                    partial_text=partial.text(),
                    sync_thread=sync_thread,
                ),
                error_message=f"Failed to finalize graph run for {thread_id}",
            )
            # Finalize the checkpoint before exposing the terminal error. The
            # client responds to this frame by re-reading state; ordering it
            # first would race that read against interrupted-turn repair.
            if run_error is not None:
                await queue.put(RunEvent("error", run_error))
            await queue.put(None)

    task = asyncio.create_task(pump())
    if not registry.register(thread_id, task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        yield RunEvent("error", RuntimeError(_CONCURRENT_RUN_NOTICE))
        return
    try:
        while True:
            frame = await queue.get()
            if frame is None:
                break
            yield frame
        # Surface a crash that happened after the sentinel was queued.
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                yield RunEvent("error", exc)
    except (asyncio.CancelledError, GeneratorExit):
        # Client disconnected: stop the graph rather than letting it run on.
        task.cancel()
        raise
    finally:
        registry.unregister(thread_id, task)
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def fork_before_message(
    *,
    graph: Any,
    thread_id: str,
    message_id: str,
) -> bool:
    """Rewind a thread's checkpoint to just before ``message_id``.

    Backs message editing. LangGraph checkpoints are append-only, so editing a
    turn means forking history rather than mutating it: find the last snapshot
    that does NOT yet contain the message, then write it back as the new head.
    A subsequent run continues from there, which is what makes the superseded
    answer disappear instead of a second one appearing beside it.

    Returns ``False`` when the message is not in this thread's history, so the
    caller can fall back to appending.
    """
    # ``checkpoint_id``/``checkpoint_ns`` must be absent: with either set,
    # aget_state_history filters to that single pinned checkpoint and the walk
    # below never finds the message.
    history_config = {"configurable": {"thread_id": thread_id}}

    snapshots = [snapshot async for snapshot in graph.aget_state_history(history_config)]
    snapshots.reverse()  # oldest first

    target_index = next(
        (
            index
            for index, snapshot in enumerate(snapshots)
            if any(
                getattr(message, "id", None) == message_id
                for message in (snapshot.values or {}).get("messages", [])
            )
        ),
        None,
    )
    if target_index is None:
        return False

    if target_index == 0:
        # Nothing precedes it: fork to an empty thread rather than reusing the
        # snapshot, whose values are an alias we must not mutate in place.
        await graph.aupdate_state(history_config, {"messages": []}, as_node="__start__")
        return True

    before = snapshots[target_index - 1]
    next_nodes = before.next or ()
    if len(next_nodes) > 1:
        logger.warning(
            "Fork point for %s has multiple next nodes %r; forking at %r only",
            message_id,
            next_nodes,
            next_nodes[0],
        )
    await graph.aupdate_state(
        before.config,
        before.values,
        as_node=next_nodes[0] if next_nodes else "__start__",
    )
    return True
