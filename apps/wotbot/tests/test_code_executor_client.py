"""Execution retries must not replay programs after an ambiguous response."""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from wotbot.clients.code_executor import CodeExecutionUncertainError, CodeExecutorClient
from wotbot.core.settings import Settings


def settings():
    return Settings(
        _env_file=None,
        internal_api_key="test-execution-policy",
        code_executor_url="http://executor.test",
        code_executor_retry_attempts=3,
        code_executor_retry_backoff_seconds=0,
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.DecodingError,
        408,
        500,
        502,
        503,
        504,
    ],
)
def test_uncertain_execution_is_never_replayed(failure):
    applied = []

    def execute(request):
        # Model an action that completed before the response failed.
        applied.append(request.content)
        if isinstance(failure, int):
            return httpx.Response(failure, json={"detail": "Execution interrupted"})
        raise failure("Response lost", request=request)

    real_client = httpx.AsyncClient
    with patch(
        "wotbot.clients.code_executor.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(execute), **kw),
    ):
        with pytest.raises(CodeExecutionUncertainError, match="may already have applied"):
            asyncio.run(
                CodeExecutorClient(settings()).execute(
                    session_id="test",
                    code="wot.invoke_action('urn:test:device', 'toggle')",
                )
            )
    assert len(applied) == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_failures_before_sending_can_retry(failure):
    attempts = 0
    applied = []

    def execute(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure("Not connected", request=request)
        applied.append(request.content)
        return httpx.Response(200, json={"ok": True, "stdout": "done"})

    real_client = httpx.AsyncClient
    with patch(
        "wotbot.clients.code_executor.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(execute), **kw),
    ):
        result = asyncio.run(CodeExecutorClient(settings()).execute(session_id="test", code="pass"))
    assert result["ok"] is True
    assert attempts == 2
    assert len(applied) == 1


def test_http_rejection_is_not_retried():
    attempts = []

    def reject(request):
        attempts.append(request)
        return httpx.Response(429)

    real_client = httpx.AsyncClient
    with patch(
        "wotbot.clients.code_executor.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(reject), **kw),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(CodeExecutorClient(settings()).execute(session_id="test", code="pass"))
    assert len(attempts) == 1


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b'{"ok": "false"}'])
def test_invalid_response_is_uncertain_and_not_retried(body):
    attempts = []

    def execute(request):
        attempts.append(request)
        return httpx.Response(200, content=body)

    real_client = httpx.AsyncClient
    with patch(
        "wotbot.clients.code_executor.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(execute), **kw),
    ):
        with pytest.raises(CodeExecutionUncertainError):
            asyncio.run(CodeExecutorClient(settings()).execute(session_id="test", code="pass"))
    assert len(attempts) == 1
