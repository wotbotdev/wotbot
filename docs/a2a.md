# Inbound A2A

WoTBot accepts inbound agent requests using the official Python A2A SDK and the
[A2A 1.0 HTTP+JSON binding](https://a2a-protocol.org/v1.0.0/specification/#11-httpjsonrest-protocol-binding).
It runs the existing graph, router, tools, and backend services. Chat, voice,
jobs, and saved panels keep their existing interfaces and storage. An A2A context
is a hidden `a2a` thread — its context ID is that thread's ID, the way a job
attaches to a thread — and it never appears in chat listings. The chat thread
routes refuse `a2a` threads, so a context ID cannot be replayed against them.

## Enable the surface

Configure the API service, then restart it:

```dotenv
A2A_ENABLED=true
REGISTRY_PUBLIC_URL=https://api.example.com
PUBLIC_UI_ORIGIN=https://wotbot.example.com
A2A_TASK_RETENTION_DAYS=30
A2A_ARTIFACT_RETENTION_DAYS=7
A2A_DOWNLOAD_URL_TTL_SECONDS=3600
A2A_MAX_ARTIFACT_BYTES=26214400
```

The public backend origin must be reachable by calling agents. The UI origin
supplies panel links. Put the normal HTTPS reverse proxy in front of
the API, preserving streaming responses. Startup applies the additive migration
`0008_add_a2a` and `0009_a2a_identity`; they preserve existing conversations, panel versions, interactive
jobs, and virtual Thing ownership. Disabling the flag removes the new routes
without deleting saved data.

`0009_a2a_identity` upgrades both historical versions of `0008`: the original
context/file tables and the later thread/executor layout. It preserves public
context IDs, graph thread IDs, paused tasks, retry records, saved panel versions,
and previously copied downloads. Old copied bytes remain downloadable until
their original expiry; new exports stay in the executor. Owner-wide message
uniqueness is enforced by a database primary key. Previously reused IDs across
multiple tasks are blocked from replay; the existing tasks remain retrievable.
Apps launched with the original SQL-backed MCP grants must reopen their panel
tool once after this upgrade. Redis grants from the later layout keep working.
Downgrading to `0008` is refused while legacy context aliases or copied bytes
remain, because that version cannot serve them.

For the local Docker development stack, port 8123 is published on loopback and
the public backend URL defaults to `http://localhost:8123`. Point an inspector
running on your host at `http://localhost:8123/.well-known/agent-card.json`.
`REGISTRY_PUBLIC_URL` overrides the advertised URL; choose an address reachable
from the inspector's process. A separate container may need
`http://host.docker.internal:8123`, depending on its network configuration.
Docker's `http://wotbot:8123` service address is only for clients sharing its
Docker network. Recreate WoTBot after changing these environment settings:
`docker compose up -d --no-deps wotbot`.

Run **one A2A API execution process per database**. A PostgreSQL advisory lease
rejects a second process before recovery can interfere with active work. The
existing job, scheduler, indexing, and voice worker roles remain separate.

Create an API key with `agent:invoke` through the existing API-key settings/API.
This scope delegates all enabled assistant capabilities, including device
actions and job creation. It does not grant direct access to credential or
source-management APIs; those keep their existing scopes. Each key owns its own
A2A contexts, tasks, downloads, and MCP resources, even when two keys belong to
the same administrator. Key revocation/expiry also ends active A2A execution;
actions already completed cannot be undone.

## Discovery and requests

| Endpoint | Purpose |
| --- | --- |
| `GET /.well-known/agent-card.json` | Public Agent Card |
| `POST /a2a/v1/message:send` | Send and wait for completion or a pause; optionally return immediately |
| `POST /a2a/v1/message:stream` | Send and stream task updates and artifacts |
| `GET /a2a/v1/tasks/{id}` | Retrieve status, messages, and artifacts |
| `GET /a2a/v1/tasks` | List the invoking key's tasks |
| `POST /a2a/v1/tasks/{id}:subscribe` | Snapshot and subsequent updates for an active task |
| `POST /a2a/v1/tasks/{id}:cancel` | Cancel execution and finalize the checkpoint |
| `GET /a2a/artifacts/{id}` | Download with an API key or temporary download token; also supports `HEAD` |

The card advertises six skills with examples and input/output types: `chat`,
`control`, `analysis`, `jobs`, `virtual_things`, and `discovery`. Send the desired
work as text or structured JSON; the existing router selects the intent. There
is no separate endpoint per skill.

```bash
curl https://api.example.com/.well-known/agent-card.json

curl --no-buffer https://api.example.com/a2a/v1/message:stream \
  -H 'Authorization: Bearer <api-key>' \
  -H 'A2A-Version: 1.0' \
  -H 'Content-Type: application/json' \
  -d '{"message":{"messageId":"energy-report-001","role":"ROLE_USER","parts":[{"text":"Analyze yesterday’s energy use and create a dashboard."}]}}'
```

The stream starts with a Task and then emits status/artifact events. The Task
provides `id` (the task ID) and `contextId`. `message:send` returns the Task in a
`task` response field; add `"configuration":{"returnImmediately":true}` beside
`message` to return while execution continues. The assistant's explanation is
in `status.message` and retained message history; generated outputs are in
`artifacts`.

An official Python SDK client can use the same card:

```python
import asyncio
import os
from uuid import uuid4

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import AgentCard, SendMessageRequest
from google.protobuf.json_format import ParseDict


async def main():
    origin = os.environ["WOTBOT_ORIGIN"].rstrip("/")
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {os.environ['WOTBOT_API_KEY']}",
                 "A2A-Version": "1.0"},
        timeout=httpx.Timeout(30, read=None),
    ) as http:
        response = await http.get(origin + "/.well-known/agent-card.json")
        response.raise_for_status()
        card = ParseDict(response.json(), AgentCard())
        client = ClientFactory(ClientConfig(
            httpx_client=http, streaming=True,
            supported_protocol_bindings=["HTTP+JSON"],
        )).create(card)
        request = ParseDict({"message": {
            "messageId": str(uuid4()), "role": "ROLE_USER",
            "parts": [{"text": "Show the current living room temperature."}],
        }}, SendMessageRequest())
        async for event in client.send_message(request):
            print(event)


asyncio.run(main())
```

Reuse the returned `contextId` for subsequent work; omit `taskId` to start a new
task after the preceding task ends. A context ID is issued by the server and
belongs to the API key that opened it; another key's context, or an invented one,
is rejected. Only one executing or paused task is admitted per context — the
database enforces this — and a paused task must be resumed or cancelled before
new work starts.

Keep a stable `messageId` when retrying a request. An identical retry returns
or subscribes to the existing task without repeating its actions. Changing
message content, context/task identity, metadata, or accepted output modes
under that ID is rejected. Switching between streaming, waiting, and immediate
return is allowed. A task's identity is derived from the message that created
it, so a retry resolves to the same task rather than starting a second one.
Deduplication lasts as long as the retained task.

Task listing supports `contextId`, `status`, `statusTimestampAfter`, `pageSize`
(up to 100), and opaque `pageToken` cursors. Results are ordered by descending
status timestamp. Pass `includeArtifacts=true` to include artifacts in list
results. `historyLength=0` suppresses history; a positive value limits it.
Pagination tokens are bound to the key and query filters.

## Pauses and task lifetime

Credentials produce `TASK_STATE_AUTH_REQUIRED`. Confirmations and other graph
interruptions produce `TASK_STATE_INPUT_REQUIRED`. These are resumable states,
not completion. The status message contains readable text and a JSON part:

```json
{
  "kind": "wotbot.input_requests",
  "requests": [{
    "requestId": "server-issued-interrupt-id",
    "kind": "confirmation",
    "explanation": "Confirm whether execution may continue.",
    "details": {},
    "responseSchema": {
      "type": "object",
      "properties": {"approved": {"type": "boolean"}},
      "required": ["approved"],
      "additionalProperties": false
    }
  }]
}
```

Reply with a new message ID and the corresponding task/context IDs. Supply one
JSON part per pending request, matching its `responseSchema`:

```json
{
  "message": {
    "messageId": "confirmation-reply-001",
    "contextId": "returned-context-id",
    "taskId": "returned-task-id",
    "role": "ROLE_USER",
    "parts": [{"data": {
      "requestId": "server-issued-interrupt-id",
      "response": {"approved": true}
    }}]
  }
}
```

Credential replies accept `{"status":"credential_saved"}` or
`{"status":"cancelled"}`. Provision the actual secret through the existing
credential API/UI first, using an independently authorized management key.
Never put credentials in conversation content. Source registration similarly
uses the existing source API and then a `source_registered` reply with
`source_id`, or `cancelled`. Other input requests accept the string described by
their schema. Arbitrary graph commands and extra reply fields are rejected.

A subscriber that stops reading is dropped rather than allowed to hold up
execution. Its final event carries `metadata.wotbotStreamTruncated`, so a
truncated stream is distinguishable from a finished one; retrieve the task and
subscribe again.

Disconnecting a stream leaves execution running. Get the current Task, then
subscribe while it is active; retrieval includes all retained artifacts. A
subscription begins with a fresh snapshot, so no SSE event ID is needed.
Terminal tasks are retrieved with GET, not subscribed to. Explicit cancellation
waits for the shared execution lifecycle and checkpoint cleanup.

After a restart, abandoned submitted/running tasks become failed with an
uncertain-outcome explanation. They are never automatically replayed because a
device action may already have happened. Intentionally paused tasks remain
resumable. Inspect device state before deciding to submit new work after an
uncertain outcome.

## Artifacts and panels

Artifacts come from successful executed tools and their matching inputs.
Explanatory assistant messages remain separate. Export errors fail the task
explicitly while preserving outputs already captured.

| Output | Representation |
| --- | --- |
| Image/chart | A typed URL part; Plotly outputs also include `wotbot.plotly` JSON with the figure |
| Exported file | Metadata (filename, MIME type, size, file expiry) plus temporary and authenticated URLs; bytes stream from the code executor |
| Structured tool result | JSON part with `kind: "wotbot.tool_result"`, tool name, and result |
| Generated panel | JSON descriptor with `kind: "wotbot.panel"` |

Artifact bytes are not copied. A download streams straight from the code
executor's artifact store, and WoTBot records only what it needs to authorize
the read and to say when the link stops working, so an A2A link never outlives
the bytes behind it. Their URL parts and `metadata.downloadUrl` contain
a temporary `?downloadToken=...` capability, so browsers and image tags can fetch
the content without an Authorization header. Anyone holding that link can read
that single artifact for one hour by default (`A2A_DOWNLOAD_URL_TTL_SECONDS`),
limited by the file's remaining lifetime. The API key itself never appears in
the link. Metadata separates `downloadUrlExpiresAt` (link expiry) from
`expiresAt` (file expiry); `authenticatedDownloadUrl` preserves the canonical
URL for clients sending `Authorization: Bearer <same-api-key>`.

Authenticated task retrieval, listing with `includeArtifacts=true`, retry
responses, and streams issue fresh links, including for existing artifacts.
Refresh/retrieve the task in an inspector when a cached link expires. Bare URLs
without a token still require the owning API key; another authenticated key
receives 404. Invalid or expired links return HTTP 410. Revoking or expiring the
owning key, or removing its `agent:invoke` scope, also disables its download
links. File expiry returns 410 and deletion returns 404. Grants are stored in
Redis with a TTL; losing Redis grants requires fresh links, without losing the
durable artifacts. WoTBot removes download tokens from its access logs; configure
any external reverse proxy to omit them from logs too.

File bytes expire after seven days by default, following the code executor's
own retention (`FILE_ARTIFACTS_TTL_SECONDS`, and `ARTIFACTS_TTL_SECONDS` for
charts and images); `A2A_ARTIFACT_RETENTION_DAYS` can only shorten a link, never
extend it past the bytes. If the executor sweeps a file first, the download
returns 410.

Tasks, their history and their retry identity expire after 30 days, including
paused tasks. Expiring the last task of a context also retires the conversation:
its hidden thread and its graph checkpoints are deleted, so retention reaches the
content and not only the bookkeeping. Cleanup runs at startup and hourly. Saved
panels and their MCP resources persist until the panel is deleted. Expired
manifests may remain available to explain unavailable downloads.

WoTBot's panel descriptor is an application JSON format carried by A2A:

```json
{
  "kind": "wotbot.panel",
  "version": 1,
  "panelId": "saved-panel-id",
  "panelVersionId": "immutable-initial-version-id",
  "panelUrl": "https://wotbot.example.com/panels?panelId=saved-panel-id"
}
```

An A2A client can display `panelUrl`. It opens the existing Panels drawer under
the deployment's normal UI access controls. Saved panels belong to the shared
Panels collection; their source A2A conversations remain hidden. The link
follows the current saved panel, including later edits, while `panelVersionId`
keeps naming the immutable version generated for this task.

Subscriptions carry their own stream position: a subscribe returns the cursor
captured before it, `things.next_subscription_event` accepts `cursor` and returns
`nextCursor`, and the bridge threads these through its poll loop. An open
subscription therefore costs no writes. Omitting the cursor resumes from the
present rather than replaying.

The bridge installs `window.wot` before generated scripts run and queues
operations until the host initializes and supplies the grant. It supports
reads, writes, actions, binary helpers, observations, subscriptions, polling,
and teardown. Every operation is checked against the saved version's
Thing/affordance/operation allowlist. Subscriptions are bound to that artifact;
a different panel cannot poll or cancel them. Reopen the tool after grant expiry.
Deleting a saved panel removes its MCP resources and invalidates its grants.

Resource metadata declares CSP requirements and requested camera, microphone,
geolocation, or clipboard permissions when applicable. Hosts may decline those
permissions. Panels can inspect `wot.hostPermissions()` and handle rejected
browser API calls; unhandled permission denial also emits `wot-error`. Features
requiring a declined capability remain unavailable in that host.

MCP validates Host/Origin headers. The configured backend host and backend/UI
origins are allowed automatically. If a browser host connects from another trusted origin, configure
`MCP_ALLOWED_ORIGINS` as a comma-separated list of exact origins; add alternate
proxy Host values through `MCP_ALLOWED_HOSTS` when needed. Keep the existing
deployment CORS/reverse-proxy policy consistent with these values.

## Validation and rollout

Install the backend's updated dependencies and run from the repository root:

```bash
.venv/bin/python -m pytest apps/wotbot/tests -m 'not integration and not external' -q
.venv/bin/python -m pytest apps/wotbot/tests/integration -q
```

Integration tests use isolated PostgreSQL/Valkey services and the official A2A
and MCP clients. They cover every router intent, ownership, deduplication,
concurrency, pauses/resumes, restart/revocation/cancellation, durable artifacts,
panel versions/allowlists/grants/subscriptions, and migration preservation.
Device and LLM responses are simulated for deterministic acceptance tests;
existing external-provider smoke tests remain opt-in.

Run UI `npm test` and `npm run typecheck` from `apps/ui`. The reproducible
[browser acceptance fixture](../apps/wotbot/tests/browser/README.md) exercises
the WoTBot panel bridge using a generated panel. Before enabling a deployment,
use its real origins and a dedicated key to exercise discovery, a read-only
task, and the intended device operations.

This release accepts text and structured JSON. Binary uploads, outbound A2A
calls, push notifications, A2UI, and multiple API execution processes are
outside its scope. The old `/api/a2a` routes, including upload and JSON-RPC
endpoints, are retired; migrate clients to the endpoints above.

## MCP execution profiles

See [MCP toolsets](./mcp.md) for `/mcp/assistant`, `/mcp/intents`, and `/mcp/raw`. A2A and the assistant/intent profiles can continue the same context owned by one API key. Raw contexts are separate. Migration `0010_mcp_toolsets` adds execution metadata and raw contexts while retaining existing IDs, stored data, and download links.
