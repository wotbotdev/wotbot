# WoTBot

WoTBot is a multi-service Web of Things assistant stack. It combines a Next.js chat and operations UI, a Python LangGraph WoTBot service, a Python code execution service, a node-wot runtime for Web of Things device access, and a virtual Thing producer for computed and record-backed Things.

## Services

```text
browser
  -> ui
  -> wotbot
     -> code-executor
     -> wot-runtime
     -> virtual-servient
     -> postgres / valkey
```

- [`apps/ui`](./apps/ui/README.md): Next.js frontend for chat, live mode, Things, jobs, settings, and backend proxying.
- [`apps/wotbot`](./apps/wotbot/README.md): FastAPI + LangGraph service for agent orchestration, Thing registry APIs, jobs, persistence, and LiveKit worker roles.
- [`apps/code-executor`](./apps/code-executor/README.md): internal Python worker service used by the agent's `run_code` tool.
- [`apps/wot-runtime`](./apps/wot-runtime/README.md): internal node-wot runtime that reads, writes, invokes, and subscribes against Thing Descriptions.
- [`apps/virtual-servient`](./apps/virtual-servient/README.md): internal node-wot producer that turns wotbot virtual Thing definitions into concrete catalog Thing Descriptions.
- [`examples/thing-descriptions`](./examples/thing-descriptions): sample Thing Description assets for local scenarios.

Docker Compose also starts Postgres with pgvector, Valkey, an RDF service, and LiveKit Server.

## Getting Started

1. Copy [`.env.example`](./.env.example) to `.env`.
2. Fill the required LLM and shared-secret values.
3. Start the stack:

```bash
docker compose up -d
```

4. Open `http://localhost:3000`.

The root Compose files are compatibility wrappers around the canonical stack in [`deploy/compose.yaml`](./deploy/compose.yaml) and its local development override in [`deploy/compose.override.yaml`](./deploy/compose.override.yaml).

## A2A and MCP

External agents can use WoTBot to discover, control, analyze, and automate Things.
Enable either connection type in `.env`, then recreate the API service with
`docker compose up -d --no-deps wotbot`:

```dotenv
A2A_ENABLED=true
MCP_ENABLED=true
REGISTRY_PUBLIC_URL=http://localhost:8123
PUBLIC_UI_ORIGIN=http://localhost:3000
```

Use backend and UI URLs reachable by your client. Create an API key with
`agent:invoke` and send it as `Authorization: Bearer <api-key>`. This grants the
agent access to enabled capabilities, including device actions and job creation.

| Connection | URL on the backend | Use it for |
| --- | --- | --- |
| A2A 1.0 HTTP+JSON | `/.well-known/agent-card.json` | Discover WoTBot and connect with an A2A client |
| MCP Streamable HTTP | `/mcp/assistant` | Ask WoTBot to handle a request; start here for most clients |
| MCP Streamable HTTP | `/mcp/intents` | Choose a specific assistant intent |
| MCP Streamable HTTP | `/mcp/raw` | Let your own agent choose and call tools directly |

A2A requests use the `A2A-Version: 1.0` header. MCP clients discover tools and
their arguments through `tools/list`; all three profiles include task and
artifact tools.

Keep the same `messageId` (A2A) or `requestId` (MCP) when retrying identical work
so actions are not repeated. Reuse the returned `contextId` to continue a
conversation. Each API key owns its tasks and conversations; raw contexts are
separate from assistant conversations, and external conversations stay out of
the chat UI. A disconnect leaves work running; use task cancellation to stop it.

Files include temporary download links; retrieve the task or artifact again to
refresh an expired link. Generated panels are saved in Panels and returned with
a `panelUrl` that opens the normal WoTBot UI under its existing access controls.

Run one API execution process per database. Additional origin and retention
settings are listed in [`.env.example`](./.env.example).

## External discovery

External catalogs live in a dedicated persistent source registry, separate from
the Thing catalog. The agent first uses `sources_search`, then searches exactly
one selected source with `discover_external`, and finally uses
`onboard_candidate` to create a resource Thing.

Built-in providers cover ToolHive, uData, bounded DCAT catalogs, the EDC v3
Management API, and direct OpenAPI 3.0/3.1 or Swagger 2.0 documents. uData portals
are detected through a generic API probe. ToolHive, EDC,
private endpoints, and sources that cannot be detected are registered explicitly
through the dedicated Sources page or API. Chat-initiated registration always
opens the same confirmation form before probing or persistence.

The source record contains provider configuration, network policy, semantic
metadata, and the required security scheme. Secret values are entered in the
source credential dialog and stored separately; they never pass through chat or
action inputs. Sources are not Thing Descriptions, are not semantically indexed
as Things, and are never created at startup.

Source search results are temporary and scoped to the conversation. Onboarding
one selected result creates one resource Thing linked to its trusted source record:
a dataset Thing for uData/DCAT, an MCP-backed Thing for ToolHive, or an asset
Thing for EDC. An OpenAPI source deterministically groups supported operations
and compiles the selected group into ordinary HTTP-backed TD actions; the raw
specification never enters model context. Generated OpenAPI Things can be
regenerated explicitly from their detail page after reviewing a bounded diff.
Each downloadable dataset distribution becomes a descriptive,
metadata-rich TD action. EDC assets with valid tx-bootstrap OpenAPI metadata
instead compile up to 30 supported operations into ordinary WoT actions. The
raw specification, deployment servers, and security definitions are not copied
into the Thing; only bounded action and schema data needed for those operations
is retained. Invoking an API action negotiates an EDC transfer, resolves the
endpoint data reference, and calls the acquired endpoint for that invocation.
EDC assets without usable API metadata expose `download_asset`; malformed
metadata also records an onboarding warning. Resource selection, negotiation,
temporary capabilities, and upstream credentials remain inside the provider
binding.

EDC Things can also be regenerated from their source. Refresh detects changes
to the metadata fingerprint, supports transitions between generated API actions
and downloadable assets, and preserves local titles, descriptions, and
manually added affordances.

The opt-in live smoke tests exercise provider registration, discovery, and
onboarding against public sources and the integration Postgres/Valkey services:

```bash
RUN_EXTERNAL_DISCOVERY_TESTS=1 .venv/bin/python -m pytest -q \
  -c apps/wotbot/pyproject.toml apps/wotbot/tests/integration/test_provider_smoke_live.py
```


## Development

The default local setup uses Docker Compose with bind mounts and hot reload where practical:

```bash
docker compose up -d --build
```

Service-specific setup, test commands, and implementation notes live in the service READMEs:

- [UI README](./apps/ui/README.md)
- [WoTBot README](./apps/wotbot/README.md)
- [Code Executor README](./apps/code-executor/README.md)
- [WoT Runtime README](./apps/wot-runtime/README.md)
- [Virtual Servient README](./apps/virtual-servient/README.md)

## Versioning

The stack uses one shared version in [`VERSION`](./VERSION). Update every service
manifest from that source of truth with:

```bash
scripts/set-version.sh 0.1.0
scripts/check-version.sh
```

Release tags should be `v<version>` (for example `v0.1.0`). CI runs the version
check before building images and rejects tag/version drift.

## Top-Level Files

- [`docker-compose.yaml`](./docker-compose.yaml): root wrapper for the default stack.
- [`docker-compose.override.yaml`](./docker-compose.override.yaml): root wrapper for local development overrides.
- [`deploy/compose.yaml`](./deploy/compose.yaml): canonical multi-service stack definition.
- [`.env.example`](./.env.example): documented environment template.
- [`VERSION`](./VERSION): shared stack version used by all service manifests.
- [`LICENSE`](./LICENSE): project license.
