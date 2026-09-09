"""FastAPI routes for chat thread metadata and history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from wotbot.core.sse import sse_with_heartbeat
from wotbot.threads.messages import checkpoint_thread_state
from wotbot.threads.models import (
    DEFAULT_THREAD_TITLE,
    CreateThreadRequest,
    ThreadKind,
    UpdateThreadTitleRequest,
)
from wotbot.threads.store import (
    create_thread,
    get_thread,
    list_threads,
    update_thread_title,
)
from wotbot.threads.store import (
    delete_thread as delete_thread_metadata,
)
from wotbot.threads.runs import RunRegistry, fork_before_message, stream_run


async def _reject_a2a_thread(thread_id: str) -> None:
    """Keep A2A conversations reachable only through the A2A API.

    New A2A context IDs are their thread IDs, and are handed to the calling
    agent. Without this guard that ID could be replayed against these routes --
    including through the UI's server-side proxy -- to read, continue or delete
    another API key's conversation.
    """
    record = await asyncio.to_thread(get_thread, thread_id)
    if record is not None and record["kind"] in {ThreadKind.A2A.value, ThreadKind.MCP_RAW.value}:
        raise HTTPException(status_code=404, detail="Thread not found")


async def _get_thread_messages_payload(
    *,
    get_checkpointer: Callable[[], Any | None],
    thread_id: str,
) -> list[dict[str, Any]]:
    checkpointer = get_checkpointer()
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="Checkpointer not ready")

    state = await checkpoint_thread_state(checkpointer, thread_id)
    messages = state["values"]["messages"]
    return messages if isinstance(messages, list) else []


async def _create_thread_record(body: CreateThreadRequest | None) -> dict[str, Any]:
    payload = body or CreateThreadRequest()
    return await asyncio.to_thread(
        create_thread,
        thread_id=payload.id,
        title=payload.title or DEFAULT_THREAD_TITLE,
        created_at=payload.created_at,
        updated_at=payload.updated_at,
    )


async def _update_thread_record(
    *,
    thread_id: str,
    body: UpdateThreadTitleRequest,
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            update_thread_title,
            thread_id=thread_id,
            title=body.title,
            force=body.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _thread_record_with_messages(
    *,
    get_checkpointer: Callable[[], Any | None],
    thread_id: str,
) -> dict[str, Any]:
    record = await asyncio.to_thread(get_thread, thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages = await _get_thread_messages_payload(
        get_checkpointer=get_checkpointer,
        thread_id=thread_id,
    )
    return {**record, "messages": messages}


async def _delete_thread_record(
    *,
    get_checkpointer: Callable[[], Any | None],
    thread_id: str,
) -> dict[str, Any]:
    checkpointer = get_checkpointer()
    if checkpointer is not None:
        await checkpointer.adelete_thread(thread_id)

    deleted = await asyncio.to_thread(delete_thread_metadata, thread_id)

    return {
        "ok": True,
        "thread_id": thread_id,
        "deleted": deleted,
    }


def create_threads_router(
    *,
    get_checkpointer: Callable[[], Any | None],
    verify_internal_api_key: Callable[[Request], None],
    get_graph: Callable[[], Any | None],
    get_settings: Callable[[], Any | None],
    run_registry: RunRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/threads", tags=["threads"])
    # Shared with the A2A router so an external agent and the chat UI cannot
    # start concurrent runs on the same thread.
    run_registry = run_registry or RunRegistry()

    def _heartbeat_timeout() -> float | None:
        settings = get_settings()
        interval = settings.sse_heartbeat_seconds if settings else 15.0
        return interval if interval and interval > 0 else None

    @router.get("")
    async def get_threads(request: Request):
        verify_internal_api_key(request)
        return await asyncio.to_thread(list_threads)

    @router.post("")
    async def post_thread(
        request: Request,
        body: CreateThreadRequest | None = Body(default=None),
    ):
        verify_internal_api_key(request)
        if body and body.id:
            await _reject_a2a_thread(body.id)

        return await _create_thread_record(body)

    @router.patch("/{thread_id}")
    async def patch_thread(
        thread_id: str,
        request: Request,
        body: UpdateThreadTitleRequest,
    ):
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        return await _update_thread_record(thread_id=thread_id, body=body)

    @router.get("/{thread_id}")
    async def get_thread_by_id(thread_id: str, request: Request):
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        return await _thread_record_with_messages(
            get_checkpointer=get_checkpointer,
            thread_id=thread_id,
        )

    @router.get("/{thread_id}/state")
    async def get_thread_state(thread_id: str, request: Request):
        """Seed ``useStream``'s ``initialValues``.

        A custom transport has no ``fetchStateHistory``, so the client cannot
        load history itself; it reads it from here.
        """
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        checkpointer = get_checkpointer()
        if checkpointer is None:
            raise HTTPException(status_code=503, detail="Checkpointer not ready")

        return await checkpoint_thread_state(checkpointer, thread_id)

    @router.post("/{thread_id}/runs/stream")
    async def post_thread_run_stream(
        thread_id: str,
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
    ):
        """Stream one graph run as SSE for ``FetchStreamTransport``.

        The client posts ``{input, context, command}``; see ``threads/runs.py``
        for the frame format it expects back.
        """
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        graph = get_graph()
        if graph is None:
            raise HTTPException(status_code=503, detail="Graph not ready")

        payload = body or {}
        frames = stream_run(
            graph=graph,
            registry=run_registry,
            thread_id=thread_id,
            input_data=payload.get("input"),
            context=payload.get("context"),
            command=payload.get("command"),
        )
        return StreamingResponse(
            sse_with_heartbeat(frames, _heartbeat_timeout()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/{thread_id}/runs/fork")
    async def post_thread_run_fork(
        thread_id: str,
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
    ):
        """Rewind this thread to just before ``message_id``, for message edits.

        The client forks first, then submits the edited text as a normal run,
        so the superseded turn is replaced rather than duplicated.
        """
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        graph = get_graph()
        if graph is None:
            raise HTTPException(status_code=503, detail="Graph not ready")

        message_id = (body or {}).get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise HTTPException(status_code=400, detail="message_id is required")

        if run_registry.is_running(thread_id):
            raise HTTPException(
                status_code=409,
                detail="Cannot edit a thread while its response is still running",
            )

        async with run_registry.thread_lock(thread_id):
            if run_registry.is_running(thread_id):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot edit a thread while its response is still running",
                )
            forked = await fork_before_message(
                graph=graph,
                thread_id=thread_id,
                message_id=message_id,
            )
        return {"thread_id": thread_id, "forked": forked}

    @router.post("/{thread_id}/runs/cancel")
    async def post_thread_run_cancel(thread_id: str, request: Request):
        """Stop the in-flight run for this thread."""
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        return {"thread_id": thread_id, "cancelled": run_registry.cancel(thread_id)}

    @router.delete("/{thread_id}")
    async def delete_thread(thread_id: str, request: Request):
        verify_internal_api_key(request)
        await _reject_a2a_thread(thread_id)

        # A finishing run synchronizes thread metadata after writing its final
        # checkpoint. Wait for that cleanup before deleting, otherwise the chat
        # can reappear immediately after a successful DELETE response.
        await run_registry.cancel_and_wait(thread_id)
        async with run_registry.thread_lock(thread_id):
            return await _delete_thread_record(
                get_checkpointer=get_checkpointer,
                thread_id=thread_id,
            )

    return router
