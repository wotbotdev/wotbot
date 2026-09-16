"""Transport-agnostic Server-Sent Events helpers.

The heartbeat exists because a slow tool call (for example, a long matplotlib
render in the code executor) can produce no output for a stretch, causing the
consuming undici client to abort the response body with
``UND_ERR_BODY_TIMEOUT`` before the final answer arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

# A comment frame. The client's SSE decoder skips any line starting with ":"
# and then discards the resulting empty event, so this is inert by design.
KEEPALIVE_FRAME = ": keepalive\n\n"


def format_sse_event(event: str, data: Any) -> str:
    """Render one SSE frame.

    ``data`` is always JSON: the client decoder calls ``JSON.parse`` on the
    accumulated data lines unconditionally, so a bare string would break it.
    """
    payload = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def format_sse_error(exc: BaseException) -> str:
    """Render the terminal ``error`` frame.

    The client constructs ``StreamError`` from this payload and stops reading,
    so shape it as ``{name, message}`` rather than a dropped socket.
    """
    return format_sse_event("error", {"name": type(exc).__name__, "message": str(exc)})


async def sse_with_heartbeat(
    frames: AsyncIterator[str],
    timeout: float | None,
) -> AsyncIterator[str]:
    """Relay pre-formatted SSE frames, emitting keepalives during silence.

    ``timeout=None`` disables the heartbeat. The stream's termination cause is
    logged with the bytes/frames sent so an abrupt close can be told apart
    (normal end vs. cancellation vs. a raised exception). On a genuine
    exception a terminal ``error`` frame is emitted so the client gets a clean
    error instead of a dropped socket.
    """
    iterator = frames.__aiter__()
    pending: asyncio.Task[str] | None = None
    sent_frames = 0
    sent_bytes = 0

    def _emit(chunk: str) -> str:
        nonlocal sent_bytes
        sent_bytes += len(chunk.encode("utf-8"))
        return chunk

    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            try:
                frame = await asyncio.wait_for(asyncio.shield(pending), timeout)
            except TimeoutError:
                yield _emit(KEEPALIVE_FRAME)
                continue
            except StopAsyncIteration:
                pending = None
                logger.info("SSE stream completed: %d frames, %d bytes", sent_frames, sent_bytes)
                break
            pending = None
            sent_frames += 1
            yield _emit(frame)
    except (asyncio.CancelledError, GeneratorExit):
        logger.warning(
            "SSE stream cancelled/closed after %d frames, %d bytes", sent_frames, sent_bytes
        )
        raise
    except Exception as exc:
        logger.exception("SSE stream raised after %d frames, %d bytes", sent_frames, sent_bytes)
        try:
            yield _emit(format_sse_error(exc))
        except Exception:  # pragma: no cover - stream already gone
            pass
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
