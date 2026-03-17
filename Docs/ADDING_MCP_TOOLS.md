# Adding MCP Tools

New tools are added via `config/mcp_config.yaml` — no Python code changes required.

---

## How the MCP registry works

At startup, `app/mcp/registry.py` reads `config/mcp_config.yaml` and initializes
each enabled server. Two transport types are supported:

| Type | How it runs | When to use |
|------|-------------|-------------|
| `internal_python` | Imported in-process | Fast; for tools written as Python modules |
| `docker_stdio` | Subprocess via Docker | Isolates dependencies; for third-party MCP servers |

All tools are registered in LiteLLM function-calling format and injected into
the agent's tool schema. The LLM chooses which tool to call — you don't write
routing logic.

---

## Adding a Docker stdio server

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

Then pull the image and restart the backend:

```bash
docker pull mcp/mcp-filesystem
./scripts/start.sh
```

The tool is now available to the agent. Check it appeared:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:8000/admin/tools -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
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
curl http://localhost:8000/admin/tools -H "Authorization: Bearer $TOKEN"
```

Response includes:
- `tools`: all enabled tools with server name and connection status
- `disabled_servers`: servers present in config but `enabled: false`
- `tool_count`: total tools visible to the agent (before persona filtering)
