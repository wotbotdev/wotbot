# Code Executor

`code-executor` is the internal Python execution service used by the wotbot agent's `run_code` tool, analysis jobs, and virtual Thing handlers. It runs Python snippets in long-lived per-session worker processes, captures stdout and chart/image artifacts, and returns compact results to `wotbot`.

## What This Service Owns

- Python execution for analysis workflows.
- Virtual Thing computed-property, computed-action, and emitted-event handler execution.
- One worker process per chat thread, job, or virtual Thing handler session.
- Session-local variables and imports that survive successful calls.
- Matplotlib, Plotly, and explicit image artifact capture.
- Cleanup of idle sessions and expired artifacts.

The UI never executes Python directly, and browsers should not call this service directly.

## Runtime Shape

```text
wotbot run_code / jobs / virtual Thing dispatcher
  -> code-executor
     -> session worker process
     -> stdout / image / Plotly artifacts
  -> wotbot tool result / job result / handler result
  -> UI artifact renderer when artifacts are present
```

Chat turns use the thread id as the code-executor session id so analysis state follows the conversation. Jobs and virtual Thing bindings use their own stable session ids.

## Session Model

Each session has its own Python worker process. On POSIX systems, execution happens in a forked child of the current good state:

- successful execution promotes the child to the new live worker
- failed execution keeps the previous good worker state
- timed-out execution terminates the child and keeps the previous good worker state

This gives the agent practical rollback behavior without resetting the whole session after every mistake.

`POST /execute` returns an explicit `ok` status. Python failures return `ok: false`,
a bounded `error` summary, and completed `wot_calls`, even when stdout is truncated.
Failed runs discard generated artifacts, records and reports. Rolling back Python
state does not undo device actions that already completed.

The backend retries only connection/pool failures before sending a request. Lost
responses, read/write transport failures and HTTP errors never automatically replay
code. An uncertain outcome asks the caller to inspect device state before retrying.
Deploy the executor and backend together: the backend requires a boolean `ok` in
execution responses and treats missing status as an uncertain outcome. Analysis
jobs record explicit Python failures as failed runs without storing output records.

## Execution Environment

Workers preload common analysis tools:

- `pandas` as `pd`
- `numpy` as `np`
- `matplotlib.pyplot` as `plt`
- `plotly.io` as `pio`
- `requests`
- `save_image(...)`
- a small `wot` helper client for runtime reads, writes, and action invocation

Installed packages are defined in [`pyproject.toml`](./pyproject.toml).

## Artifacts

Artifacts are written under `ARTIFACTS_DIR`, defaulting to `/tmp/code-executor-artifacts`.

- `plt.show()` captures the current Matplotlib figure as a PNG.
- `fig.show()` captures Plotly figures as JSON that can be rendered by the UI.
- `save_image(...)` stores PIL images, bytes, and file-like images as PNG artifacts.
- `save_artifact(data, filename=..., mime_type=...)` exports bytes, UTF-8 text, or
  an open binary stream. Serialize tables explicitly, for example:

  ```python
  save_artifact(df.to_csv(index=False), filename="data.csv", mime_type="text/csv")
  ```

  Files are staged during execution and published by the supervising process
  only after success. Failed, cancelled and timed-out runs discard their staged
  files. Same-session executions are serialized so their pipe responses and
  files cannot be mixed. Results contain descriptors (`id`, filename, MIME type,
  byte size, SHA-256 and expiry), without embedding file bodies.
- A cleanup task removes idle sessions and artifacts older than `ARTIFACTS_TTL_SECONDS`.

Artifact cleanup is TTL-based rather than tied directly to thread deletion.

Exports use `FILE_ARTIFACTS_TTL_SECONDS` (default seven days from publication), independently of
charts, images and HTML previews (one hour). Reads do not extend retention;
expired exports are unavailable even before cleanup removes their bytes. Limits
default to 64 MiB per file, 16 files per run, and 256 MiB total per run.

The authenticated `/artifacts/{id}/metadata` route describes an artifact;
`/artifacts/{id}/content` returns its original bytes as an attachment. The existing
`/artifacts/{id}` route also serves new exports as attachments, including JSON
and HTML, while legacy charts retain their preview behavior. The UI streams
downloads and displays filenames, sizes and expiry in chat, jobs and live mode.

Compose stores artifacts on the `executor-artifacts` volume at
`/tmp/code-executor-artifacts`, preserving the path used by uploaded attachments.
Build updated executor images before using this volume, so its directory belongs
to `appuser` in both development and production. Existing temporary previews and
uploads in an old container's `/tmp` are not migrated; preserve any needed files
before recreating that container. Standalone deployments should point
`ARTIFACTS_DIR` at persistent storage if exports must survive service replacement.

These are app downloads under the existing deployment access model. Portable MCP
download tickets and `wot.download_action` are separate interfaces and are not
required by `save_artifact`.

## Security Boundary

This service is safer than running code in the main agent process, but it is not a strong sandbox or micro-VM.

Current protections include per-session worker processes, rollback on failure, selected secret removal from the execution environment, a non-root production image, read-only Compose configuration, dropped Linux capabilities, `no-new-privileges`, and CPU, memory, and PID limits.

Keep it on an internal network. User code still runs through Python `exec(...)`, can import installed packages, and can make network requests if the container network allows them.

## Development

### With Docker Compose

```bash
docker compose up -d code-executor
```

The dev override builds the `builder` stage, bind-mounts the package, exposes port `8888`, and runs Uvicorn with reload.

### Directly

```bash
cd apps/code-executor
pip install -e .
uvicorn code_executor.api.app:app --host 0.0.0.0 --port 8888 --reload
```

## Environment

Settings are defined in [`code_executor/models/settings.py`](./code_executor/models/settings.py). The main groups are session limits, execution timeout, artifact storage and TTL, logging, internal auth, and WoT runtime access.

## Important Files

- [`code_executor/api`](./code_executor/api): FastAPI app, routes, cleanup loop, and auth dependencies.
- [`code_executor/session_pool.py`](./code_executor/session_pool.py): async session orchestration.
- [`code_executor/worker.py`](./code_executor/worker.py): worker process lifecycle and rollback behavior.
- [`code_executor/execution_environment.py`](./code_executor/execution_environment.py): globals, stdout capture, and artifact capture.
- [`code_executor/wot_client.py`](./code_executor/wot_client.py): WoT runtime helper exposed to executed code.
- [`code_executor/models`](./code_executor/models): settings and request/response schemas.
- [`Dockerfile`](./Dockerfile): container image.

## Current Gaps

- The automated test suite is still small and currently focuses on web artifact behavior.
- Artifact cleanup is TTL-based rather than thread-scoped.
- The service favors practical isolation and rollback over strong sandbox guarantees.
