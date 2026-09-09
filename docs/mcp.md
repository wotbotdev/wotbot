# MCP toolsets

WoTBot exposes three MCP Streamable HTTP endpoints. Choose one endpoint for each
connection; the profile cannot be changed within that connection. They run in
the existing WoTBot API service on port 8123.

| Endpoint | Tools |
| --- | --- |
| `/mcp/assistant` | `ask_wotbot`: the assistant chooses the intent |
| `/mcp/intents` | `intent.chat`, `intent.control`, `intent.analysis`, `intent.jobs`, `intent.virtual_things`, `intent.discovery` |
| `/mcp/raw` | Direct device, catalog, discovery, code, panel, job, and virtual-Thing tools; no assistant LLM |
| `/mcp/apps` | Existing generated-panel tools and resources |

All three execution profiles also provide `task.get`, `task.list`, `task.resume`,
`task.cancel`, `artifact.get`, `artifact.list`, generated `panel.open.<artifactId>`
tools, and the app-only `panels.call`. Raw connections additionally provide
`subscription.poll`. A tool belonging to another profile cannot be called by
name. New internal tools are private until explicitly added to the public catalog.

## Configuration and authentication

```dotenv
MCP_ENABLED=true
A2A_ENABLED=true
REGISTRY_PUBLIC_URL=http://localhost:8123
PUBLIC_UI_ORIGIN=http://localhost:3000
```

`MCP_ENABLED` defaults to false and enables the three execution profiles plus
`/mcp/apps`. `A2A_ENABLED` independently enables A2A and continues to enable
`/mcp/apps` for existing deployments. Downloads and panel assets work with either
flag. Disabling a flag does not delete data.

Use an API key with `agent:invoke`, passed as `Authorization: Bearer <api-key>`.
This is full delegation to the assistant's enabled capabilities, including writes,
actions, code execution, and job creation. Direct credential and source-management
APIs retain their existing scopes. Each key owns its contexts, tasks, artifacts,
launch grants, and raw subscriptions, even when keys belong to one administrator.
The deployment's Thing catalog, jobs, and saved Panels collection retain their
existing sharing rules.

Docker reads these settings from the deployment's `.env` file. Recreate the
WoTBot service after changes: `docker compose up -d --no-deps wotbot`. No new
container or port forward is needed. The default local forward is loopback port
8123. Use a public backend origin reachable by the client; `wotbot:8123` works
only inside the Docker network.

When deploying this code update with the development Compose configuration,
rebuild both services with `docker compose up -d --build wotbot wot-runtime`.
The runtime update provides isolated subscription namespaces and handle checks;
all public MCP endpoints still belong to WoTBot on port 8123.

Run one API execution process per database. A2A and MCP share one execution lease,
run registry, task store, recovery process, and retention sweep. Existing
`A2A_TASK_RETENTION_DAYS`, `A2A_ARTIFACT_RETENTION_DAYS`,
`A2A_DOWNLOAD_URL_TTL_SECONDS`, and `A2A_MAX_ARTIFACT_BYTES` apply to both surfaces.
Task/retry records default to 30 days; file retention never exceeds the executor's
actual expiry. Download grants default to one hour. Saved panels persist until deleted.

## Calls, tasks, and conversations

These are ordinary MCP `tools/call` arguments, not native MCP Tasks requests.

```json
{
  "name": "ask_wotbot",
  "arguments": {
    "requestId": "energy-report-001",
    "message": "Analyze yesterday's energy use and create a dashboard.",
    "waitSeconds": 25
  }
}
```

Assistant and intent tools accept `message` text and/or `data` JSON. All execution
calls require a unique `requestId` (1–200 characters). Repeating an identical
request ID returns the existing task; changing its content is rejected. Keep the
original request unchanged when retrying. The wait duration does not affect retry
identity. This does not make a new request ID safe for repeating device actions.

The tool returns readable text plus `structuredContent` containing `taskId`,
`contextId`, `status`, `messages`, `statusMessage`, `result`, `artifacts`, `pending`,
and `error`. Statuses include `submitted`, `working`, `input_required`,
`auth_required`, `completed`, `failed`, and `canceled`. A paused or still-running
task is a successful admission, not a completed operation.

Calls wait up to 25 seconds by default. `waitSeconds: 0` returns immediately;
the maximum is 30 seconds. To wait again:

```json
{"name":"task.get","arguments":{"taskId":"<task-id>","waitSeconds":25}}
```

Reuse `contextId` for a subsequent request. A2A, assistant, and intent profiles
can continue the same owned assistant context. An intent call selects its initial
assistant branch; configured handoffs remain available. `ask_wotbot` always
returns to automatic routing. Raw contexts are separate and cannot be used for
assistant calls. All these histories are API-only and unavailable through chat
thread routes.

One task may execute or remain suspended per context. New work in a busy context
is rejected. A stream disconnect, MCP request cancellation, or expired wait ends
only the wait. Explicitly stop execution using:

```json
{"name":"task.cancel","arguments":{"taskId":"<task-id>"}}
```

Cancellation waits for checkpoint cleanup but cannot undo completed actions.
After restart, abandoned running tasks fail with an uncertain-outcome explanation.
Paused tasks remain resumable. Actions are never automatically replayed after a
crash. Revoking the invoking key stops its active execution.

`task.list` takes optional `contextId` and `cursor`; it lists only the profile's
execution family and authenticated owner, 50 records at a time. Pass the returned
`nextCursor` unchanged with the same filters. `artifact.list` similarly supports
`taskId`, `contextId`, and `cursor`.

## Pauses

A paused result includes every pending request's identifier, explanation, kind,
and JSON response schema. Submit all replies together, with a fresh retry ID:

```json
{
  "name": "task.resume",
  "arguments": {
    "taskId": "<task-id>",
    "requestId": "confirm-report-001",
    "replies": [{"requestId":"<pending-request-id>","response":{"approved":true}}]
  }
}
```

For credential challenges, provision the credential through the existing
credential API, then respond `{"status":"credential_saved"}` or
`{"status":"cancelled"}`. Do not include credentials in tool arguments or
conversation content. Source-registration pauses similarly require the existing
source API and return its source ID. Arbitrary graph commands are rejected.

## Raw tools

Raw tool names and argument schemas come from the explicitly registered existing
tool contracts. `tools/list` is the authoritative schema, including nested types.
Execution controls sit outside the tool's `arguments`:

```json
{
  "name":"wot_read_property",
  "arguments":{
    "requestId":"temperature-001",
    "arguments":{"thing_id":"urn:room:sensor","property_name":"temperature"}
  }
}
```

Server-generated contexts isolate Python sessions and discovery candidates.
Reuse the returned raw `contextId` to retain that state. Clients cannot set
internal configuration, thread IDs, ownership, or job creation provenance.
`run_code` retains its existing executor behavior and capabilities; the raw
profile does not use an LLM to choose or rewrite operations.

Observation/subscription tools return an owned subscription ID and initial
`cursor`. Poll and retain each returned `nextCursor`:

```json
{
  "name":"subscription.poll",
  "arguments":{
    "contextId":"<raw-context-id>",
    "subscriptionId":"<subscription-id>",
    "cursor":"<previous-cursor>",
    "timeoutMs":25000
  }
}
```

Binary events retain their content type and base64 payload. Polling renews a
one-hour idle lease. Use `wot_remove_subscription` in the same context to stop.
Subscriptions cannot be accessed across owners or contexts. Their runtime
namespace also prevents removal from stopping a UI, panel, or another client's
observation. Expired or lost subscriptions return an error requiring explicit
creation of a new subscription.

## Artifacts and panels

`artifact.get` takes `artifactId` and returns the manifest, structured result or
panel descriptor, and fresh temporary download links where applicable. Files
remain in the code executor. Browser links use `/agent/artifacts/<id>?downloadToken=...`;
canonical URLs require the API key. Existing `/a2a/artifacts/<id>` URLs and grants
continue to work. Expired content returns HTTP 410.

Tool results include resource links to artifact manifests. Images also travel as
MCP image content when they fit the one-MiB combined preview budget; larger images
remain available through their download links. Plotly JSON and other structured
results remain attached to the task.

Generated panels are saved once, with an immutable initial version. Their
WoTBot link opens the current saved panel, while the MCP App uses that immutable
version. Relist tools to discover its `panel.open.<artifactId>` tool, then call it
on the current connection. A2A panel descriptors continue to point to `/mcp/apps`.
The existing Apps bridge, one-hour launch grants, saved capability allowlist,
binary operations, and subscriptions are shared by all profiles. Deleting the
panel invalidates its MCP resources and launch grants.
