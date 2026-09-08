# Internal tool contracts

The chat and background-job graphs retain their existing tool names and routing.
Their tools use `agent/tools/contracts.py` to advertise closed top-level argument
schemas and reject unknown arguments before calling a handler. Pydantic validates
declared bounds and variants. Input errors identify the offending fields and
explain how to retry, without printing supplied values. Handler-side validation
errors do not claim that no operation ran.

Only the outer argument object is closed. Thing Descriptions, JSON-LD extensions,
record schemas, handler state and other declared mapping inputs retain their
existing nested schemas. Framework-injected config and routing IDs remain hidden
from models; external source registration retains its public `config` alias and
the existing approval/credential UI.

Search tools advertise their limits instead of silently clamping invalid calls:

| Tool | Bounds |
| --- | --- |
| `things_search` | `k`: 1–20 |
| `things_list` | `page`: 1–1,000,000; `per_page`: 1–200 |
| `things_sparql` | `limit`: 1–500 |
| `describe_rdf_schema` | `limit`: 1–200 |
| `sources_search`, `discover_external` | `limit`: 1–25; query length: at most 500 |

`things_search` returns ranked catalog metadata by default. Set
`include_summary=true` for the longer indexing prose. This changes only the tool
response: search ranking, stored index content and REST/UI search remain intact.
Inspect a Thing or affordance for verified names, schemas and units; search
matches do not establish those details or exhaustive catalog counts.

`run_code` exports files through `save_artifact`; chat and jobs preserve their
metadata while the UI streams the bytes from the executor. See the
[executor artifact contract](../../code-executor/README.md#artifacts).

These changes adapt the contract and artifact work from `feature/mcp-cleanup`
to the existing LangChain tools. They do not require an MCP server. The execution
failure and retry contract must be deployed with matching backend and executor
versions, as described in the executor README.
