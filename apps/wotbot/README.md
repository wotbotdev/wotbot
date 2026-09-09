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

The opt-in A2A 1.0 HTTP+JSON interface uses the official SDK and the existing
LangGraph assistant. One Agent Card advertises `chat`, `control`, `analysis`,
`jobs`, `virtual_things`, and `discovery`; the router selects the intent.

`A2A_ENABLED=true` enables the public `/.well-known/agent-card.json`, authenticated
`/a2a/v1` execution/task routes, and `/a2a/artifacts` downloads. API keys need
`agent:invoke`, which delegates all enabled assistant capabilities. Contexts,
tasks, and downloads are isolated by API-key ID. A2A threads remain hidden from chat.
File and image outputs include temporary download links usable by browsers
without an API key. Links expire after one hour by default; retrieving the task
refreshes them. Canonical download URLs still require the owning API key.

The shared execution lifecycle lives in `core/agent_runs.py`. Chat retains its
SSE adapter in `threads/runs.py`; A2A consumes typed events directly. Durable A2A
records live in `agent_api/models.py`. The single `0008_agent_execution`
migration creates the shared A2A/MCP schema after `0007_add_thing_origin`. Generated
panels use the existing panel service and immutable initial panel versions.

See the [connection and rollout guide](../../docs/a2a.md) for examples, ownership,
pauses, artifact formats, and retention. The experimental
`/api/a2a` routes and handwritten protocol models have been removed.

## Persistence And Migrations

The application schema is owned by Alembic migrations in [`src/wotbot/migrations`](./src/wotbot/migrations). They ship inside the `wotbot` package so `alembic upgrade head` resolves them in every install mode. App startup calls `alembic upgrade head`, so API, worker, LiveKit, and indexer processes share the same schema path.

Migration versions intentionally skip `0002`; the dropped revision was superseded before release, and the remaining chain starts at `0001` then continues with `0003`.

The unreleased A2A/MCP revisions `0008`–`0011` were consolidated into
`0008_agent_execution`. Its schema matches the former `0011_agent_message_family`
head. For a development database already at that head, stop application processes
and adopt the new revision from `apps/wotbot` without changing stored data:

```bash
python -m alembic -c src/wotbot/alembic.ini stamp --purge 0008_agent_execution
python -m alembic -c src/wotbot/alembic.ini check
```

Databases on earlier intermediate feature revisions must first reach
`0011_agent_message_family` using the pre-squash checkout. Databases at `0007` or
earlier use the normal upgrade command. Downgrading the feature migration is
refused while agent contexts, tasks, artifacts, or subscriptions remain.

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
