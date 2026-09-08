# WoT Runtime

`wot-runtime` is the internal Node.js service that executes Web of Things operations for WoTBot. It uses node-wot bindings to read properties, write properties, invoke actions, and manage subscriptions against Thing Descriptions stored by `wotbot`.

## What This Service Owns

- Runtime interaction with Things through node-wot.
- Thing Description lookup through the wotbot registry service.
- Credential retrieval through the internal service boundary.
- Runtime event publication to Redis streams for automation jobs.
- Subscription management and runtime health reporting.

It does not own Thing registry persistence. `wotbot` remains the source of truth for Things, credentials, search, jobs, and API keys. Virtual and record-backed Things arrive as normal catalog Thing Descriptions produced by `virtual-servient`; this service does not keep a separate virtual-record dispatch path.

## Runtime Shape

```text
wotbot agent/jobs/code-executor
  -> wot-runtime
     -> node-wot servient
     -> devices / Thing affordances
     -> Redis event stream
```

The service is kept on the backend network in Docker Compose. `wotbot` and `code-executor` call it through internal service URLs.

For HTTP and provider-backed GET/HEAD actions, pass path and query parameters in `uri_variables`. If none are supplied, the runtime can recover matching fields from an object passed as `input`, using the Thing's declared `uriVariables`. It drops the request body and uses the resolved variables for both invocation and caching. The runtime resolves an available form before preparing the request and keeps cached results separate for each form. Explicit URI variables, POST bodies, and non-HTTP binding inputs keep their existing meaning.

Responses that declare JSON or tabular data but contain an HTML error page fail with `invalid_response` (HTTP 502), including previously cached pages. The check uses the original response bytes, so a valid JSON string containing HTML remains data. Declared HTML/XML responses, plain text, and unknown binary formats remain supported.

## Development

### With Docker Compose

```bash
docker compose up -d wot-runtime
```

The dev override builds the dependency stage, bind-mounts the app, and runs `tsx watch`.

### Directly

```bash
cd apps/wot-runtime
npm install
npm run dev
```

Use `npm run build` for TypeScript compilation, `npm run lint` for ESLint, and `npm run start` to run the compiled app.

## Environment

The Docker image defaults to:

- `REGISTRY_URL=http://wotbot:8123`
- `REDIS_URL=redis://valkey:6379`
- `WOT_RUNTIME_STREAM=wot_runtime_events`

Compose also sets `HOST`, `PORT`, and runtime tokens from the root environment. See [`src/config/env.ts`](./src/config/env.ts) and the root [`.env.example`](../../.env.example).

## Important Files

- [`src/index.ts`](./src/index.ts): service startup.
- [`src/config/env.ts`](./src/config/env.ts): environment parsing.
- [`src/http/runtime-routes.ts`](./src/http/runtime-routes.ts): HTTP route registration.
- [`src/runtime/servient.ts`](./src/runtime/servient.ts): node-wot servient setup.
- [`src/runtime/operations.ts`](./src/runtime/operations.ts): Thing operation execution.
- [`src/runtime/credentials.ts`](./src/runtime/credentials.ts): credential resolution.
- [`src/services/form-selection.ts`](./src/services/form-selection.ts): TD form selection for runtime operations.
- [`src/services/cache.ts`](./src/services/cache.ts): optional Thing Description cache.
- [`src/services/thing-catalog-client.ts`](./src/services/thing-catalog-client.ts): wotbot registry client.
- [`src/services/stream-publisher.ts`](./src/services/stream-publisher.ts): Redis stream publishing.
- [`src/services/subscriptions.ts`](./src/services/subscriptions.ts): subscription lifecycle.

## Contributor Notes

- Keep registry persistence in `wotbot`; this service should stay focused on runtime operations.
- Keep credentials behind internal service calls and avoid exposing raw credential data in logs.
- Prefer adding node-wot bindings through the runtime setup rather than scattering protocol setup across handlers.
