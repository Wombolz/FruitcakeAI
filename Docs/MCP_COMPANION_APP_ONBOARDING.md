# Fruitcake MCP Companion App Onboarding

Use this sheet when starting a new MCP server or interactive companion app for
Fruitcake. It is the short implementation guide; the normative contract remains
[`Docs/_internal/fruitcake_mcp_extension_contract_v1.md`](./_internal/fruitcake_mcp_extension_contract_v1.md).

## The Goal

A Fruitcake companion owns its domain data, behavior, persistence, tools, and
optional web UI. Fruitcake owns model orchestration, identity, policy,
approvals, presentation, artifact persistence, and auditing.

Build the companion as a portable MCP server first. Fruitcake enhancements must
remain optional so its core workflows still work in a headless MCP client or a
different MCP Apps host.

## Choose a Conformance Level

### Level 0: Headless MCP (required)

Every project must provide:

- standard MCP tools and, where useful, resources
- valid object-shaped JSON Schema for every tool input
- accurate tool annotations, especially `readOnlyHint`
- concise, truthful text in every tool result's `content`
- core workflows that do not require a custom UI

### Level 1: Portable MCP App (for interactive UI)

In addition to Level 0:

- link an entry tool to a stable `ui://` resource with
  `_meta.ui.resourceUri`
- serve the resource as `text/html;profile=mcp-app`
- implement the standard `ui/initialize`, data-delivery, and teardown lifecycle
- communicate over the host-provided JSON-RPC `postMessage` bridge
- use standard `tools/call` for UI-initiated tool calls
- adapt to supplied theme, locale, timezone, dimensions, and display modes
- retain a useful text fallback when interactive UI is unavailable

### Level 2: Fruitcake-enhanced (optional)

Fruitcake can persist `core.mcp_app` launch artifacts and present apps inline,
in a transient drawer, or in a detached macOS window. Do not make domain
correctness depend on these Fruitcake-specific surfaces.

## Current Fruitcake Host Capabilities

Implemented today:

- MCP tool discovery and execution
- internal Python, stdio, Docker stdio, Streamable HTTP, and legacy POST-only
  HTTP transports
- nested MCP Apps metadata and bounded legacy metadata compatibility
- discovery and authenticated loading of linked same-server `ui://` resources
- validation of `text/html;profile=mcp-app` resources
- sandboxed MCP App hosting with initialize, tool-input, tool-result,
  size-change, open-link, and teardown messages
- model-visible and app-only tool visibility
- same-server app tool calls with schema validation, policy checks, and audit
  records
- automatic bounded calls to explicitly read-only app tools for trusted
  first-party servers
- host-owned approval and exact one-shot replay for permitted app-requested
  mutations
- durable `core.mcp_app` metadata with provenance and safe fallback content
- inline, transient drawer, and detached-window presentation
- tool-list refresh notifications for standard SDK transports
- connection diagnostics through `/admin/tools` and
  `/admin/mcp/diagnostics`

Important current bounds:

- MCP App HTML resource: 512 KB
- app-initiated tool request: 20 KB
- app-initiated tool result: 96 KB
- dynamic inline height: 260–720 points
- artifact inline JSON payload: 64,000 encoded bytes
- artifact fallback text: 24,000 characters
- artifact resources: 12
- artifact actions: 8

Do not assume unrestricted bridge capabilities, direct networking, filesystem
access, browser credentials, frames, forms, or persistent web storage. Use
capability detection and fail safely.

## Runtime Data Refresh Is Not Tool Refresh

Values returned by tools such as `list_styles`, `list_devices`, or
`list_watchlists` are data, not tool definitions. When approved styles change:

- keep the `list_styles` tool definition stable
- call `list_styles` again when the app opens, resumes, the user refreshes, or
  the companion's freshness window expires
- return stable style IDs and preferably a revision or `updated_at` value
- reconcile a selected style by ID and handle a removed style explicitly
- do not send `notifications/tools/list_changed` just because the rows returned
  by `list_styles` changed

Fruitcake uses `notifications/tools/list_changed` to rediscover tools when tool
names, schemas, annotations, visibility, or UI linkage change. It does not
currently guarantee resource-change subscriptions for companion data, so the
app must remain correct by re-querying a read-only data tool.

## Recommended Project Shape

Keep transport, domain logic, and UI separate:

```text
companion/
├── server/              MCP initialization, tools, and resources
├── domain/              Domain logic and persistence
├── ui/                  Self-contained MCP App HTML/CSS/JavaScript
├── tests/               Schema, tool, resource, lifecycle, and policy tests
├── README.md             Setup, transport, permissions, and limitations
└── manifest.yaml         Optional project metadata; not authorization
```

The server may expose local and remote transports, but both should use the same
domain behavior and tool schemas.

## Design the Tools First

Separate plain data, UI launch, read operations, and mutations. For example:

```text
inventory_get_devices       model-visible read tool
open_inventory_dashboard    model-visible MCP App entry tool
inventory_app_get_page      app-only read tool
inventory_update_device     app-only or model-visible mutation
```

Every tool definition needs:

- an unambiguous name within the server
- a concise description that tells the model when to call it
- an object-shaped `inputSchema` with `additionalProperties: false` when
  appropriate
- accurate MCP annotations
- `_meta.ui` only when the tool is associated with an MCP App

Read-only example:

```json
{
  "name": "inventory_get_devices",
  "description": "List devices and their current health state.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "site_id": { "type": "string" }
    },
    "required": ["site_id"],
    "additionalProperties": false
  },
  "annotations": {
    "readOnlyHint": true
  }
}
```

Do not mark a tool read-only if it refreshes persistent state, triggers a job,
controls equipment, sends a message, writes a file, or changes an external
system. Make consequential operations idempotent where practical.

## Add an MCP App Entry Tool

Use one stable resource URI and the canonical nested metadata:

```json
{
  "name": "open_inventory_dashboard",
  "description": "Open the interactive inventory dashboard.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "site_id": { "type": "string" }
    },
    "additionalProperties": false
  },
  "annotations": {
    "readOnlyHint": true
  },
  "_meta": {
    "ui": {
      "resourceUri": "ui://inventory/dashboard.html",
      "visibility": ["model", "app"]
    }
  }
}
```

The result separates compact model context from bounded UI state:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Loaded 18 devices for the North Campus; two need attention."
    }
  ],
  "structuredContent": {
    "site": { "id": "north", "name": "North Campus" },
    "summary": { "total": 18, "warning": 2 },
    "devices": []
  }
}
```

Do not put the HTML document in the tool result. Serve it through
`resources/list` and `resources/read`. Do not claim Fruitcake displayed the UI;
the server can only truthfully report that it returned the data.

## Add App-Only Tools Carefully

Use app-only visibility for refresh, pagination, selection, and other UI calls
that should not consume model tool context:

```json
{
  "annotations": { "readOnlyHint": true },
  "_meta": {
    "ui": {
      "resourceUri": "ui://inventory/dashboard.html",
      "visibility": ["app"]
    }
  }
}
```

Fruitcake does not treat app-only visibility as authorization. It validates
that the tool belongs to the same server, links to the same resource, matches
its input schema, and is permitted by server trust and user policy.

For a mutation, omit `readOnlyHint: true` and declare destructive or idempotent
semantics accurately where supported. Fruitcake presents approval in native
host UI, outside the app sandbox, and replays the stored intent exactly once
after approval. The app must tolerate the approval pause and must never present
its own confirmation as authoritative host approval.

Trusted first-party status is not standing pre-approval. Fruitcake v1 has no
policy that lets a trusted companion perform frequent model- or app-initiated
mutations without per-intent approval. Autonomous task pre-approval is separate,
applies to one task run, and does not create a reusable exemption for a server
or tool. Design frequent operations as read-only when they truly are; otherwise
make the mutation cheap to review, bounded, idempotent where practical, and able
to resume after one-shot approval.

## Build the UI Resource

The initial UI resource must:

- have a stable `ui://` URI
- use `text/html;profile=mcp-app`
- be linked by a tool on the same server
- contain no secrets, bearer tokens, or Fruitcake credentials
- tolerate multiple instances, teardown, and later reinitialization
- remain usable at compact inline and expanded window sizes
- use negotiated capabilities instead of host-name detection
- use host-mediated link opening
- use tools or scoped resources for large or current datasets

Prefer a compact, self-contained bundle. Treat all remote content as data, not
executable markup. Fruitcake's sandbox does not grant direct network,
filesystem, database, cookie, or host-DOM access.

## State, Identity, and Fallbacks

- The companion owns stable IDs and durable domain state.
- Fruitcake owns Fruitcake user, session, and approval identity.
- Never trust a user ID supplied by browser JavaScript.
- Do not use conversation history as the canonical domain datastore.
- Reopened apps should query current state instead of assuming bootstrap data
  is still fresh.
- Every tool must remain useful through concise text when UI support is absent.
- Large files and binaries belong in scoped resources, not base64 tool results.

## Register the Server in Fruitcake

Add the companion to `config/mcp_config.yaml`. Prefer standard Streamable HTTP
for a new HTTP companion:

```yaml
mcp_servers:
  inventory:
    type: streamable_http
    url: http://127.0.0.1:47840/mcp
    headers_from_env:
      Authorization: INVENTORY_MCP_AUTHORIZATION
    enabled: true
    timeout: 60
    classification: first_class_mcp
    shipping_default: false
    trust_boundary:
      first_party: true
      uses_network: true
      shell_execution: false
      writes_disk: false
```

Available transport types are:

| Type | Use |
|---|---|
| `internal_python` | In-process first-party Python tools |
| `stdio` | Local MCP subprocess |
| `docker_stdio` | Isolated optional/developer MCP subprocess |
| `streamable_http` | New standards-compliant HTTP MCP server |
| `http` | Compatibility only for existing POST-only companions |

Keep secrets in the backend environment, not in YAML. Set every trust-boundary
field truthfully. New optional companions should normally use
`shipping_default: false`.

## Verification Before Handoff

Test all of the following:

- [ ] Core workflows succeed in a headless MCP client.
- [ ] Every tool has valid schema, description, visibility, and annotations.
- [ ] Read tools do not mutate or trigger consequential work.
- [ ] Every result includes concise and truthful text content.
- [ ] Structured content and resources stay within documented bounds.
- [ ] The entry tool links the exact same-server `ui://` resource.
- [ ] The resource returns `text/html;profile=mcp-app`.
- [ ] Initialize, tool-input, tool-result, resize, link, and teardown flows work.
- [ ] The UI works at compact and expanded sizes and in light/dark themes.
- [ ] App-only calls reject wrong-server, wrong-resource, and invalid-schema
      requests.
- [ ] Mutations pause for native approval; denial does not execute them.
- [ ] Approved mutations execute the stored intent only once.
- [ ] Restart, disconnect, timeout, and stale-data behavior are explicit.
- [ ] The text fallback remains useful when the UI cannot load.
- [ ] `/admin/tools` shows the expected tools and server ownership.
- [ ] `/admin/mcp/diagnostics` reports a healthy connection.
- [ ] Relevant automated tests pass.
- [ ] The README documents setup, auth, ports, permissions, data ownership,
      external dependencies, and known limitations.

Before calling an extension portable, test it in Fruitcake and at least one
independent MCP or MCP Apps host.

## Common Failure Modes

- Building the UI before defining small, truthful tools and results.
- Returning a huge dashboard payload to the model on every interaction.
- Marking refresh, control, or write operations as read-only.
- Using app-only visibility as if it were authorization.
- Embedding secrets or API tokens in HTML or structured content.
- Depending on Fruitcake-specific bridge messages instead of standard MCP Apps.
- Making the chat transcript the source of truth for domain state.
- Assuming an iframe or detached window will live forever.
- Shipping optional or third-party integrations enabled by default.
- Using the legacy `http` adapter for a new server instead of Streamable HTTP.

## Canonical References

- [Fruitcake MCP Extension Contract v1](./_internal/fruitcake_mcp_extension_contract_v1.md)
- [Adding MCP Tools](./ADDING_MCP_TOOLS.md)
- [Fruitcake Artifact Contract v1](./_internal/artifact_contract_v1.md)
- [StocksApp MCP App Migration Plan](./_internal/stocksapp_mcp_app_migration_plan.md)
- [Security Baseline](./SECURITY_BASELINE.md)
- [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview)

When this sheet conflicts with the normative extension contract or current
implementation tests, follow the normative contract and tested implementation,
then update this sheet in the same change.
