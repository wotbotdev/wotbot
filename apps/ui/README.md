# UI

`ui` is the Next.js frontend for WoTBot. It owns the browser experience and keeps backend services behind server-side proxy routes.

## What This App Owns

- Chat and embedded chat experiences rendered with assistant-ui and streamed
  directly from LangGraph.
- Live mode controls for microphone, camera, agent dispatch, transcripts, and artifacts.
- Sidebar thread navigation, search, rename, and delete flows.
- Thing registry views for listing, creating, uploading, inspecting, and credential management.
- Automation job list, creation, detail, run history, conversation, and notification views.
- Settings screens for API key management.
- Server-side proxying to `wotbot`, `code-executor`, and other internal service APIs.

The UI does not persist the active conversation itself. The `chatId` route
parameter, LangGraph SDK `threadId`, and backend LangGraph checkpointer share
the same thread identity.

## Runtime Shape

```text
browser
  -> Next.js UI
  -> server-side proxy routes
  -> wotbot / code-executor
```

The browser talks to the Next.js app. The Next.js app forwards internal requests to backend services using environment-configured service URLs and shared internal credentials.

## Development

### With Docker Compose

```bash
docker compose up -d ui
docker compose exec ui npm run lint
docker compose exec ui npm run typecheck
docker compose exec ui npm run test
```

The dev override bind-mounts the app and preserves container-owned `node_modules` and `.next`.

### Directly

```bash
cd apps/ui
npm install
npm run dev
```

Use `npm run build` for a production build and `npm run start` to serve it.

## Version Label

The sidebar version label comes from `NEXT_PUBLIC_APP_VERSION` at build time. The publish workflow injects a release tag for tagged builds and the short commit SHA for branch builds. Local Docker builds can override it with:

```bash
APP_VERSION="$(git describe --tags --always --dirty)" docker compose up -d --build ui
```

## Environment

Most UI settings are backend URLs and shared internal credentials. See [`src/lib/backend-env.ts`](./src/lib/backend-env.ts), [`src/lib/app-version.ts`](./src/lib/app-version.ts), and the root [`.env.example`](../../.env.example).

### Reasoning Effort

`REASONING_EFFORT_ENABLED`, `REASONING_EFFORT_LEVELS` (comma-separated), and `REASONING_EFFORT_DEFAULT` control the reasoning-effort selector in the full chat toolbar ([`src/components/wotbot/chat-route/reasoning-effort-select.tsx`](./src/components/wotbot/chat-route/reasoning-effort-select.tsx); parsing lives in [`src/lib/reasoning-effort.ts`](./src/lib/reasoning-effort.ts)). The selector is hidden entirely unless enabled and at least one level is configured; it's absent from embedded chat by design. The selected level is submitted as ordinary LangGraph state on every subsequent run and persists in `localStorage` across reloads.

The chat page reads these values on the server from the container's runtime environment and passes a serialized configuration to the client. The UI and backend therefore use the same shared variables from the root `.env`; the published UI image does not need to be rebuilt for a different selector configuration. The backend still independently validates every requested level against its allow-list.

### Embedded Chat

The embedded chat is available at `/embed/chat`. It creates an ephemeral chat session and cleans up best-effort when the page unloads.

The route supports initial prompt parameters:

```text
/embed/chat?prompt=Show%20the%20warehouse%20throughput
/embed/chat?prompt=Show%20the%20warehouse%20throughput&autosubmit=1
```

Add `jobEvents=0` to suppress the global job notification event stream
(`/api/jobs/events`) for embedded chat pages:

```text
/embed/chat?jobEvents=0
```

The disabled values are `0`, `false`, `no`, and `off`.

The route also accepts runtime prefill messages from its parent frame:

```ts
iframe.contentWindow?.postMessage(
  {
    type: 'deck:prefill',
    prompt: 'Show the warehouse throughput',
    submit: true,
  },
  'https://ui.example',
);
```

Configure trusted parent origins with the runtime environment variable:

```bash
EMBED_CHAT_ALLOWED_ORIGINS=https://deck.example,http://localhost:8080
```

Only exact `http` and `https` origins are accepted. Wildcards, opaque `null` origins, invalid URLs, and non-HTTP protocols are ignored. The embed page is rendered dynamically, so this value is read at UI server runtime rather than at image build time.

## Important Files

- [`src/app`](./src/app): Next.js routes and server-side route handlers.
- [`src/components/wotbot`](./src/components/wotbot): chat, live mode, tool-call cards, and artifact rendering.
- [`src/components/jobs`](./src/components/jobs): automation job list, form, detail, run, and conversation UI.
- [`src/components/things`](./src/components/things): Thing registry screens and credential dialogs.
- [`src/components/settings`](./src/components/settings): settings panels.
- [`src/lib`](./src/lib): backend clients, stream helpers, deletion flow, formatters, and tests.
- [`src/hooks`](./src/hooks): browser-side hooks for job events and live media.

## Contributor Notes

- Keep backend service calls in server-side helpers or route handlers.
- Keep `chatId`, LangGraph SDK `threadId`, backend `thread_id`, and
  code-executor session ids aligned.
- Keep Next.js route handlers thin; business logic belongs in shared libs or backend services.
- Preserve thread-delete cleanup across chat metadata, LangGraph state, and code-executor sessions.
- Prefer focused component tests around parsing, formatting, streaming, and cleanup behavior.
