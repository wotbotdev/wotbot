FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml .
COPY code_executor/ code_executor/
RUN pip install --no-cache-dir .
RUN useradd -m appuser && mkdir -p /tmp/code-executor-artifacts && chown appuser:appuser /tmp/code-executor-artifacts
USER appuser

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY code_executor/ code_executor/

RUN useradd -m appuser && mkdir -p /tmp/code-executor-artifacts && chown appuser:appuser /tmp/code-executor-artifacts
USER appuser

ENTRYPOINT ["uvicorn", "code_executor.api.app:app", "--host", "0.0.0.0", "--port", "8888"]
