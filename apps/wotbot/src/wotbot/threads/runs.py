"""Chat SSE adapter for the shared graph execution lifecycle."""

from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from typing import Any

from fastapi.encoders import jsonable_encoder

from wotbot.core.agent_runs import (
    RunRegistry as RunRegistry,
    fork_before_message as fork_before_message,
    stream_run_events,
)
from wotbot.core.sse import format_sse_error, format_sse_event


def _event_name(mode: str, namespace: Sequence[str] | None) -> str:
    return "|".join((mode, *(namespace or ())))


async def stream_run(**kwargs: Any) -> AsyncIterator[str]:
    async with aclosing(stream_run_events(**kwargs)) as events:
        async for event in events:
            if event.mode == "error":
                yield format_sse_error(event.payload)
            else:
                yield format_sse_event(
                    _event_name(event.mode, event.namespace), jsonable_encoder(event.payload)
                )
