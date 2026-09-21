# WoTBot

`wotbot` is the Python agent service behind WoTBot. It builds the LangGraph assistant, owns the Thing registry and automation job backend, persists conversation and registry state, and provides worker roles used by the stack.

## What This Service Owns

- LangGraph agent assembly, routing, prompts, and tool binding.
- Backend API composition for chat transport, threads, Things, virtual Things, credentials, API keys, jobs, panels, media, and speech.
- Postgres persistence for LangGraph checkpoints, thread metadata, Thing registry data, virtual Thing definitions, credentials, API keys, search vectors, panels, and jobs.
- Redis-backed job events, Thing event outbox delivery, and scheduling coordination through Taskiq.
- LiveKit agent worker integration for local voice and camera-assisted sessions.
- The worker entrypoints for job execution, scheduling, Thing indexing, and LiveKit.

It is intended to run on an internal network behind `ui`.

## Runtime Shape

```text
ui
  -> wotbot FastAPI app
     -> LangGraph agent
     -> code-executor for run_code
     -> wot-runtime for Thing operations
     -> virtual-servient for produced virtual Thing TDs
     -> rdf-service for SPARQL/RDF graph queries
     -> Postgres / Valkey
```

The frontend `chatId`, LangGraph SDK `threadId`, backend LangGraph `thread_id`,
and code-executor session id are intentionally the same value so chat
continuity stays aligned across services.

## Agent Architecture

The graph is assembled in [`src/wotbot/agent/builder.py`](./src/wotbot/agent/builder.py). A router selects a branch, then the selected branch alternates between an LLM node and its allowed tools.

```text
START
  -> router
     -> respond -> respond_tools -> respond -> END
     -> control_llm -> control_tools -> control_llm -> END
     -> analysis_llm -> analysis_tools -> analysis_llm -> END
     -> jobs_llm -> jobs_tools -> jobs_llm -> END
     -> virtual_things_llm -> virtual_things_tools -> virtual_things_llm -> END
```

- `chat`: lightweight conversational responses.
- `control`: device discovery and control.
- `analysis`: device/data analysis plus Python execution.
- `jobs`: automation job creation, inspection, debugging, manual runs, and deletion.
- `virtual_things`: standalone computed properties/actions and emitted virtual events backed by `wotbot.virtual_things` and produced by `virtual-servient`.

Prompts live in [`src/wotbot/agent/prompts`](./src/wotbot/agent/prompts). Tool grouping lives in [`src/wotbot/agent/tool_groups.py`](./src/wotbot/agent/tool_groups.py).

## Jobs And Live Media

Automation jobs use the same graph and persistence model as normal conversations, with hidden per-job checkpoint threads when a job needs user input. Structured-record jobs persist record rows in `wotbot.jobs.records` and register record-backed bindings through the generic virtual Thing path. The Docker stack runs separate `job-worker` and `job-scheduler` processes from the same image.

Live voice uses a self-hosted LiveKit Server plus the `wotbot livekit-agent` worker. The worker handles realtime media, speech-to-text, text-to-speech, interruption handling, transcription forwarding, and the bridge back into the LangGraph assistant.

## A2A and MCP

See [A2A and MCP setup](../../README.md#a2a-and-mcp) for connection URLs,
authentication, and how to retrieve results.

Both interfaces run in this API service. Shared execution and persistence live
in [`agent_api`](./src/wotbot/agent_api); [`a2a`](./src/wotbot/a2a) and
[`mcp`](./src/wotbot/mcp) adapt their respective protocols. Assistant calls use
the existing LangGraph assistant; raw MCP calls execute tools directly. Generated
panels use the existing panel service and are returned as links to the UI.

## Data attachments for generated panels

`create_web_interface` accepts an optional `data` mapping from attachment names
to artifact IDs returned by `run_code`. Export the computed result with
`save_artifact(..., mime_type="application/json")` or
`mime_type="application/geo+json"`. Panel JavaScript reads the decoded value
synchronously with `window.panelData.read("areas")`; `panelData.names()` lists
the available names. Each read returns a fresh copy.

The backend verifies the export size/checksum and strict JSON, stores the
original UTF-8 content, and delivers it separately from the agent-authored HTML.
The model sees only references and metadata. There is no coordinate copying,
extra browser network permission, or executor extension. A panel with attached
data can omit Thing capabilities; live Thing calls still require their usual
capability declarations. Limits are eight attachments and 8 MiB combined.

The tool returns reusable `panel-data-...` references in its `data` field.
The UI and API/MCP pin these resolved references, not the expiring source file
IDs. Pinning retains snapshots for every saved panel version, including after
source export expiry or chat deletion. Edits receive references and HTML, never
the attached content. Version restoration restores the corresponding references.
Omitting `data` during an AI edit preserves the current references automatically;
an explicit mapping replaces them. Snapshots share the existing operator/panel
access scopes. Temporary snapshots
expire with their source export and are pruned on subsequent imports; deleting
a panel removes snapshots no other panel or version uses. Existing panels and
Thing bridges remain compatible. Startup applies migration `0009_panel_data`;
downgrading refuses to discard data attached to saved panels.

## Panel browser validation

Generated panels, including live Thing and mixed live/attachment panels, must
pass a Chromium initialization check before `create_web_interface` stores or
returns their HTML artifact. The tool sends the
exact wrapped document and attached snapshots to the separate `panel-validator`
service. A failed check returns structured diagnostics to the agent. An unavailable
validator, timeout or unavailable external dependency withholds the artifact and
is reported separately from broken panel code.

The default WoTBot image includes Playwright, headless Chromium and its system
dependencies. The validator uses that same image with a separate command and
non-root user; it has no separate Dockerfile or image publication.
The image installs only Chromium's headless shell. A local Linux ARM64 build on
2026-09-21 grew from 857 MB to 1.63 GB in unpacked layers: about 770 MB for
Playwright, the browser and system dependencies. Services share these image layers.
Start it with `docker compose up --build -d panel-validator`. Compose configures
`PANEL_VALIDATOR_URL` for the backend and agent workers. For a backend running on
the host, use `PANEL_VALIDATOR_URL=http://localhost:8919` with the development
Compose override, or install `.[browser]`, run `playwright install chromium`, and
start the service:

```bash
uvicorn wotbot.panels.browser_validator:app --host 127.0.0.1 --port 8919 \
  --ws-max-size 134217728
```

The worker runs a new sandboxed Chromium process for each request, one request at
a time, with a 25-second hard deadline. It checks runtime/console errors, resource
failures, a completed nonempty `panelChecks` verdict, and a uniform blank screenshot
at 1000×650. After one second of observation it captures that viewport, resizes
the same panel to 390×650, observes another second and captures it again. This
does not reload the panel or repeat initial property reads. Each attempt saves its
diagnostics, self-checks, document hash and captured PNGs in Postgres. The chat's
**View validation** control shows the attempt history, saved reports and screenshots;
it also appears on the delivered panel. Screenshot bytes stay out of the generating
conversation and tool results. No user interaction testing is performed.
The readiness result is read-only from the moment the wrapper loads. A browser
pass requires individual passing assertions and both screenshots; a status label
without that evidence is rejected.
Direct manual panel API edits do not go through this creation-time gate.
Saved-panel AI edits return validation evidence on both success and failure.
The drawer shows the latest edit's warnings, diagnostics, attempt history and
screenshots. Failed edits preserve the saved panel; applying a manual edit or
restoring a version clears the previous AI edit's feedback from the drawer.

An independent model call reviews both viewport screenshots for empty content,
missing map tiles, rendered errors, clipped labels and overlap. Its findings are
**advisory**: definite and uncertain findings appear as warnings; they never
change the browser verdict or trigger automatic repair. Reviewer outages and
incomplete responses appear as unavailable, never as a clean review. This does
not verify facts, geography, underlying data, content below the viewport or controls.

`PANEL_VISUAL_REVIEW_ENABLED=true` enables this review by default when
`OPENAI_MODEL_SUPPORTS_VISION=true`. It reuses the configured model endpoint/key;
`PANEL_VISUAL_REVIEW_MODEL` optionally selects a different image-capable model on
that endpoint. An explicit override declares image support for that model.
The call gets only the two screenshots and a fixed rubric, with no generating
conversation, HTML or raw attachments. Visible device values can appear in those
images. Credentials remain in the backend/worker, never the validator. There is
one request, no provider retries, a 30-second deadline and a 2500-token output
limit. Each report records model, rubric version, latency, token usage and cost
when the provider reports it. Token counts are not billed cost.

Each panel has one initial attempt and at most two repairs in a user turn.
Accounting uses checkpointed tool history, so changing a title or attachment does
not reset a failed attempt. Static and attachment failures count too. Inconclusive
or unavailable checks stop retries immediately, and simultaneous panel calls are
rejected so they cannot race the limit. After success, a separate panel can start
its own budget; a new user message also starts a fresh budget. Direct programmatic
calls without conversation history return no automatic retry permission.
Missing or expired export IDs are repairable attachment failures; executor
connection failures and server outages stop retries as unavailable.

Migration `0010_panel_validation_reports` adds evidence storage;
`0011_panel_narrow_screenshot` adds the second PNG. Reports expire
after 30 days, become inaccessible on expiry and are pruned on subsequent saves.
Deleting their source chat also deletes them. A report is limited to 128 KiB and
each PNG to 4 MiB. Saving evidence must succeed before delivery. Read endpoints
`/api/panel-validation/{id}` and `/api/panel-validation/{id}/screenshot` use the
existing panel read scope and private, uncached responses. The screenshot endpoint
accepts `?viewport=normal` (default) or `?viewport=narrow`. External A2A/MCP
conversation evidence is excluded from these UI routes. Temporary panel-edit
reports without a saved chat use the same expiry. Reports are collected for new
attempts only; screenshots from earlier chats cannot be recovered retroactively.

Live panels read real property values through a restricted bridge. A private
WebSocket connects the validator to the calling backend/agent process, which
keeps the runtime credentials and invokes only `read_property`. Both ends enforce
the panel's declared Thing IDs, property names and `readProperty` capability;
URI variables and decoded JSON/binary values follow the UI bridge's contract.
The check allows at most 20 reads, with a five-second timeout per read within the
overall deadline. Runtime failures make validation inconclusive even if the
panel catches the error. Raw property responses are not copied into the tool
result; visual findings may quote text visible in the screenshots.

Writes, actions, property observations and event subscriptions are blocked during
validation and listed as untested in the result. Attempting them during loading
makes the verdict inconclusive and withholds the artifact. Controls are never
clicked: a panel can pass initialization with an action button whose interaction
remains untested. The agent is instructed to use reads for initial state and
wait until rendering finishes before calling `panelChecks`.

Chromium retains its sandbox. The container runs without backend credentials
or persistent storage. Its read relay cannot invoke runtime mutations. Its CSP
matches the UI's policy, and network interception permits only the approved HTTPS dependencies,
checking redirect targets before fetching them. The worker's two synthetic origins
match the production iframe's cross-origin relationship and sandbox flags; they
do not reproduce deployment-specific permissions or authentication. The
[Playwright seccomp profile](https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json)
is vendored in `deploy/panel-validator-seccomp.json` (license in the sibling
`panel-validator-seccomp.LICENSE`), with `chroot` additionally
allowed so Chromium can enter its own sandbox while container capabilities are
dropped. Hosts must support unprivileged user namespaces; browser launch failures
fail validation instead of disabling the sandbox.
Use the root Compose entrypoint, or set `PANEL_VALIDATOR_SECCOMP_PROFILE` to the
profile's absolute path when using a different Compose project directory.

See [browser tests](tests/browser/README.md) for regression commands.

## Persistence And Migrations

The application schema is owned by Alembic migrations in [`src/wotbot/migrations`](./src/wotbot/migrations). They ship inside the `wotbot` package so `alembic upgrade head` resolves them in every install mode. App startup calls `alembic upgrade head`, so API, worker, LiveKit, and indexer processes share the same schema path.

Migration versions intentionally skip `0002`; the dropped revision was superseded before release, and the remaining chain starts at `0001` then continues with `0003`.

Run migrations manually from `apps/wotbot` when needed (the config lives in the package, so pass it with `-c`):

```bash
python -m alembic -c src/wotbot/alembic.ini upgrade head
python -m alembic -c src/wotbot/alembic.ini current
python -m alembic -c src/wotbot/alembic.ini check
```

LangGraph checkpoints use `AGENT_STATE_DATABASE_URL` when set, otherwise `REGISTRY_DATABASE_URL`. Keep `SEARCH_VECTOR_DIMENSIONS` stable for an existing database because the pgvector column is migrated with that dimension.

## Development

### With Docker Compose

```bash
docker compose up -d wotbot
docker compose exec wotbot sh -lc "cd /app && python -m pytest tests"
```

The dev override builds the local image, bind-mounts source, migrations, and tests, and runs the API with reload.

### Directly

```bash
cd apps/wotbot
pip install -e ".[dev]"
wotbot serve --reload
```

Other local process roles:

```bash
wotbot job-worker
wotbot job-scheduler
wotbot thing-indexer
wotbot rdf-service
wotbot livekit-agent start
```

Container-backed integration tests start disposable pgvector Postgres and Valkey services:

```bash
.venv/bin/python -m pip install -e "apps/wotbot[test]"
.venv/bin/python -m pytest -c apps/wotbot/pyproject.toml \
  apps/wotbot/tests/integration -m integration
```

## Environment

The root [`.env.example`](../../.env.example) documents required and optional settings. Runtime settings are defined in [`src/wotbot/core/settings.py`](./src/wotbot/core/settings.py). [`src/wotbot/core/config.py`](./src/wotbot/core/config.py) keeps the legacy cached `get_settings()` import path.

Common groups:

- LLM and embedding settings, including the main model's image-input capability.
- Shared internal API keys and registry tokens.
- Postgres, pgvector, and LangGraph checkpoint configuration.
- Redis, Taskiq jobs, WoT runtime, virtual-servient, RDF service, and event stream settings.
- LiveKit, speech-to-text, and text-to-speech settings.
- Code-executor URL, timeout, and retry settings.
- Reasoning-effort settings (see below).

### Live camera context

Set `OPENAI_MODEL_SUPPORTS_VISION=true` when the model behind `OPENAI_MODEL`
accepts image inputs. While a LiveKit camera feed is active, WoTBot freezes one
current frame at the first foreground model call of a user turn and reuses it
throughout that turn. The image is prompt-only and is never written to chat
history or checkpoints. There is no separate vision model or camera-analysis
tool. The model is told to ignore the frame for unrelated requests.
`CAMERA_FRAME_MAX_DIMENSION` and `CAMERA_FRAME_JPEG_QUALITY` control the
pre-encoded frame size.

This applies to voice mode only. Frames live in a per-process in-memory
registry that only the LiveKit agent worker writes to, so the API process that
serves text chat never has one to attach — asking about the camera from the
chat pane reaches a model with no image, even while live mode is running.

### Reasoning effort

`REASONING_EFFORT_ENABLED`, `REASONING_EFFORT_LEVELS` (comma-separated allow-list), and `REASONING_EFFORT_DEFAULT` let a reasoning-capable model (o-series, gpt-5, etc. — whatever's behind `OPENAI_MODEL`/`OPENAI_API_BASE_URL`) be told how hard to think. The full chat UI reads these same variables from its runtime environment, so the shared root `.env` controls both services without rebuilding the UI image. `REASONING_EFFORT_DEFAULT`, when set, becomes the baseline `reasoning_effort` on every LLM call ([`src/wotbot/core/llm.py`](./src/wotbot/core/llm.py)); the chat UI can additionally request a level per turn as plain LangGraph state. The respond/control/analysis/jobs/virtual_things branches honor it only when the requested level is in `REASONING_EFFORT_LEVELS` (see `_resolve_reasoning_effort` in [`src/wotbot/agent/nodes.py`](./src/wotbot/agent/nodes.py)). Disabled by default. There's no standardized way to query which levels a given model/endpoint actually supports — this allow-list is how an operator declares it.

`REASONING_EFFORT_STYLE` controls *how* the resolved level is sent, since not every backend speaks the OpenAI field the same way: `openai` (default) sends the level as the `reasoning_effort` request field. `qwen` instead sets Qwen's own `enable_thinking` chat-template flag via `extra_body` — vLLM is supposed to translate `reasoning_effort` into that automatically, but it's known to be unreliable for Qwen3.5 specifically ([vllm-project/vllm#35574](https://github.com/vllm-project/vllm/issues/35574)), so this talks to the model natively instead. The mapping lives in [`src/wotbot/core/reasoning_effort.py`](./src/wotbot/core/reasoning_effort.py). Qwen's switch is binary, not graduated: the literal level `"none"` means thinking off, every other configured level means on — so a Qwen deployment typically only needs `REASONING_EFFORT_LEVELS=none,think` (or similar) rather than the full OpenAI-style scale.

## Security Boundary

`wotbot` assumes an internal-service deployment model. Public traffic should terminate at `ui`, with `wotbot`, `code-executor`, `wot-runtime`, `virtual-servient`, `rdf-service`, Postgres, and Valkey kept off the public internet.

Shared internal credentials protect service-to-service calls when configured. Registry API keys are intended for registry management and search, not direct unrestricted device control.

## Important Files

- [`src/wotbot/api/main.py`](./src/wotbot/api/main.py): FastAPI app composition.
- [`src/wotbot/cli.py`](./src/wotbot/cli.py): process role entrypoint.
- [`src/wotbot/agent`](./src/wotbot/agent): graph builder, nodes, prompts, tools, and voice adapter.
- [`src/wotbot/catalog`](./src/wotbot/catalog): Thing registry, credentials, validation, and event outbox.
- [`src/wotbot/jobs`](./src/wotbot/jobs): job definitions, runs, scheduler integration, records, events, and stores.
- [`src/wotbot/media`](./src/wotbot/media): LiveKit token, dispatch, and media helpers.
- [`src/wotbot/panels`](./src/wotbot/panels): generated panel rendering, persistence, versions, and edit helpers.
- [`src/wotbot/rdf`](./src/wotbot/rdf): RDF graph indexing and SPARQL query service.
- [`src/wotbot/speech`](./src/wotbot/speech): text-to-speech and transcription proxy routes.
- [`src/wotbot/threads`](./src/wotbot/threads): thread metadata, message loading, titles, and routes.
- [`src/wotbot/search`](./src/wotbot/search): embedding and vector search for Things.
- [`src/wotbot/thing_indexer`](./src/wotbot/thing_indexer): Thing indexing worker.
- [`src/wotbot/virtual_things`](./src/wotbot/virtual_things): virtual Thing definitions, bindings, validation, dispatch, and record-backed registration.
- [`src/wotbot/workers`](./src/wotbot/workers): process role implementations.
- [`src/wotbot/migrations`](./src/wotbot/migrations): Alembic migrations.

## Contributor Notes

- Keep the LangGraph stream wire contract and run lifecycle in
  `wotbot.threads.runs`.
- Keep thread ids aligned across UI, LangGraph, and code execution.
- Keep prompts concise and prefer clearer tool boundaries over longer instructions.
- Treat schema changes as migration changes and verify with `alembic check`.
- Keep direct device/runtime access behind internal services.
