# Adding MCP Tools

New tools are added via `config/mcp_config.yaml` — no Python code changes required.

Alpha shipping policy:
- only clearly necessary first-party/internal MCPs ship enabled by default
- Docker/third-party MCPs are optional and should be left disabled unless the operator explicitly needs them
- developer-only integrations may remain documented, but they are not part of the default alpha distribution surface

---

## How the MCP registry works

At startup, `app/mcp/registry.py` reads `config/mcp_config.yaml` and initializes
each enabled server. The official MCP Python SDK handles standard transports:

| Type | How it runs | When to use |
|------|-------------|-------------|
| `internal_python` | Imported in-process | Fast; for tools written as Python modules |
| `stdio` | Local subprocess through the SDK | Node, Python, or other local MCP executables |
| `docker_stdio` | Docker subprocess through the SDK | Isolates dependencies; existing Docker configurations still work |
| `streamable_http` | Standard MCP endpoint through the SDK | HTTP servers with JSON or SSE responses and negotiated session handling |
| `http` | Legacy POST-only JSON-RPC adapter | Existing FieldKit and FruitcakeImageLab companion servers |

All tools are registered in LiteLLM function-calling format and injected into
the agent's tool schema. The LLM chooses which tool to call.

That does not mean every configured MCP is appropriate to ship enabled. Before enabling a server, classify it as:
- `core` — first-party, required for the shipped product
- `optional` — useful but not required
- `developer-only` — local/admin tooling that should stay off in the default alpha config

---

## Adding a local stdio server

Fruitcake launches the executable directly, with no shell command interpolation.
`command`, `args`, `cwd`, and `timeout` configure the process. The SDK inherits
its standard minimal environment; use `env` for literal variables and `env_from`
to copy named variables from the backend environment.

```yaml
mcp_servers:
  local_service:
    type: stdio
    command: /absolute/path/to/python
    args: ["-m", "my_mcp_server"]
    cwd: /absolute/path/to/project
    env:
      SERVICE_MODE: local
    env_from:
      SERVICE_TOKEN: MY_SERVICE_TOKEN
    enabled: true
    timeout: 60
```

Missing referenced environment variables prevent the connection. Keep secrets
in the backend environment rather than committing them to YAML.

### Elgato Stream Deck

The configured `elgato` entry launches `npx --yes @elgato/mcp-server@0.1.7`.
This version was verified with read-only tool discovery. The first launch may
need network access to download the package; later launches use npm's cache.
Node.js 18+ and `npx` must be available on the backend's PATH. For a service with
a restricted PATH, use the absolute path to `npx` and include Node's directory
in the configured process environment.

Run Stream Deck 7.4+ on the same Mac or Windows machine, enable **MCP Deck** in
Preferences, and place the actions you want exposed in the **MCP Actions**
profile. Provide descriptions for those actions. The bridge uses local IPC,
so running it inside a Linux Docker container is not equivalent to running it
alongside Stream Deck.

Elgato is explicitly enabled in this operator configuration, but marked
`shipping_default: false`. Other deployments should disable it unless needed.
The trust metadata reflects what exposed actions may do; it does not itself
enforce permissions. Existing persona restrictions and approval rules still
apply to the discovered tool names.

## Adding a Streamable HTTP server

```yaml
mcp_servers:
  remote_service:
    type: streamable_http
    url: https://mcp.example.com/mcp
    headers_from_env:
      Authorization: MY_MCP_AUTHORIZATION
    enabled: true
    timeout: 60
```

`MY_MCP_AUTHORIZATION` contains the full header value, such as `Bearer ...`.
Literal non-secret headers can be supplied through `headers`. The URL is the
exact MCP endpoint, including its path. The SDK handles protocol negotiation,
JSON/SSE responses, session headers where required, and session termination.
Interactive OAuth onboarding is not provided by this configuration; use an
existing token/header or a server that does not require it.

Keep FieldKit and FruitcakeImageLab on `type: http` until those companion servers
implement standard MCP HTTP transport. That compatibility adapter continues to
send their existing POST-only JSON-RPC requests.

## Lifecycle and tool changes

- SDK sessions have one owner task, allowing chat and scheduled tasks to call
  them concurrently and allowing shutdown from another task.
- Tool-list notifications refresh the registry, including additions and removals.
  Paginated tool lists are collected before replacing the visible catalog.
- Failed calls are never automatically repeated: a timed-out action may already
  have executed. A later independent call can reconnect to the server.
- A server unavailable at initial startup has no discovered tools; fix its setup
  and restart Fruitcake. Legacy HTTP companions require restart for tool changes.
- Use `/admin/tools` and `/admin/mcp/diagnostics` to inspect connection state,
  connection errors, and the bounded subprocess stderr tail. Configuration
  validation failures are also logged during backend startup.

---

## Adding a Docker stdio server

Prefer this only for optional or developer-only integrations. Core alpha capability should favor first-party/internal MCPs or first-party backend-owned tools when possible.

Docker MCP servers are the easiest way to add third-party tools.

**Example: add a filesystem tool**

```yaml
# config/mcp_config.yaml

mcp_servers:
  filesystem:
    type: docker_stdio
    image: mcp/mcp-filesystem
    enabled: true
    timeout: 60
```

The bundled shell server now emits standard newline-delimited MCP JSON. Rebuild
existing `fruitcake/mcp-shell` images after this upgrade:

```bash
docker build -t fruitcake/mcp-shell -f mcp_shell_server/Dockerfile .
```

For other servers, pull the image and restart the backend:

```bash
docker pull mcp/mcp-filesystem
./scripts/start.sh
```

The tool is now available to the agent. Check it appeared:

```bash
TOKEN=$(curl -s -X POST http://localhost:30417/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:30417/admin/tools -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Adding an internal Python server

Internal servers are Python modules with two required functions:

```python
# app/mcp/servers/my_tool.py

def get_tools() -> list[dict]:
    """Return MCP tool schemas."""
    return [
        {
            "name": "my_tool_name",
            "description": "What this tool does and when the agent should use it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "Description of param1"},
                },
                "required": ["param1"],
            },
        }
    ]

async def call_tool(tool_name: str, arguments: dict, user_context=None) -> str:
    """Execute the tool and return a plain string result."""
    if tool_name == "my_tool_name":
        result = do_something(arguments["param1"])
        return str(result)
    return f"Unknown tool: {tool_name}"
```

Register it in config:

```yaml
# config/mcp_config.yaml
mcp_servers:
  my_tool:
    type: internal_python
    module: app.mcp.servers.my_tool
    enabled: true
```

Restart the backend — no other changes needed.

---

## Blocking tools per persona

To prevent a tool from appearing for a specific persona (e.g., block web search
for the restricted assistant), add its exact function name to `blocked_tools` in
`config/personas.yaml`:

```yaml
# config/personas.yaml
personas:
  restricted_assistant:
    blocked_tools: [web_search, fetch_page, my_tool_name]
```

Blocked tools are removed from the LLM's tool schema entirely — the model
never sees them, so it can never call them regardless of the prompt.

---

## Disabling a server without removing it

```yaml
mcp_servers:
  filesystem:
    type: docker_stdio
    image: mcp/mcp-filesystem
    enabled: false   # ← disabled; restart to take effect
```

---

## Checking tool status

```bash
curl http://localhost:30417/admin/tools -H "Authorization: Bearer $TOKEN"
```

Response includes:
- `tools`: all enabled tools with server name and connection status
- `disabled_servers`: servers present in config but `enabled: false`
- `tool_count`: total tools visible to the agent (before persona filtering)

For alpha operators:
- if a deployment does not actively need an MCP, leave it disabled
- prefer first-party core tools for task mutation, secrets, API-backed requests, and place lookup instead of adding overlapping MCPs

## Trust boundary reminder

MCP enablement does not bypass Fruitcake's task approval model.

- If a task execution path reaches a tool that is approval-gated, the task still pauses in `waiting_approval` before the mutation executes.
- Operators can inspect that pause through:
  - `/tasks/*` task and step responses (`waiting_approval_tool`, `waiting_approval_reason`)
  - `/admin/task-runs/{id}/inspect`
  - the Fruitcake MCP inspect tools such as `fruitcake_inspect_task_run`
- When reviewing MCP additions, treat any new persistent or external mutation surface as a trust-boundary question first. If it should mutate user data, external state, or trusted catalogs, it should either reuse the current approval boundary or justify why it is safe without it.
