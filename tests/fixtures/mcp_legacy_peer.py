"""Handshake-era peer with paginated discovery and list-change notifications."""
import json
import sys

changed = False

def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method, params = message["method"], message.get("params", {})
    if "id" not in message:
        continue
    request_id = message["id"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}},
                  "serverInfo": {"name": "legacy-fixture", "version": "1"}}
    elif method == "tools/list":
        name = "second" if params.get("cursor") else ("replacement" if changed else "first")
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}]}
        if not params.get("cursor"):
            result["nextCursor"] = "page2"
    elif method == "tools/call":
        changed = True
        send({"method": "notifications/tools/list_changed"})
        result = {"content": [{"type": "text", "text": "changed"}]}
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
        continue
    send({"id": request_id, "result": result})
