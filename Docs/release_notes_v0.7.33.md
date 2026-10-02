# Release Notes v0.7.33

## Summary

This release modernizes Fruitcake's MCP transport layer around the official MCP Python SDK while preserving compatibility with existing Fruitcake companion applications.

## Included Changes

- added official SDK support for local stdio, Docker stdio, and Streamable HTTP MCP servers
- retained a dedicated legacy POST-only HTTP adapter for FieldKit, FruitcakeImageLab, and other existing companion apps
- added authenticated Streamable HTTP configuration through environment-backed headers
- added paginated tool discovery and live catalog refresh for servers that announce tool-list changes
- made SDK transport ownership safe across chat, scheduler, and shutdown tasks
- prevented automatic replay of timed-out or cancelled tool mutations
- bounded subprocess diagnostics and redacted configured environment and header secrets
- added an optional pinned Elgato Stream Deck MCP bridge configuration
- converted the bundled shell MCP server to standard newline-delimited MCP framing
- expanded MCP setup, trust-boundary, lifecycle, and diagnostics documentation

## Notes

- existing companion apps configured as `type: http` continue through the compatibility adapter and do not need to adopt Streamable HTTP immediately
- standard remote MCP endpoints should use `type: streamable_http`; local executables should use `type: stdio`
- the Elgato integration is explicitly enabled in this operator configuration but remains marked `shipping_default: false`
- failed external tool calls are not automatically replayed because a timed-out mutation may already have executed

## Verification

- `.venv/bin/pytest tests/test_mcp.py tests/test_mcp_transports.py tests/test_shell_mcp_server.py -q`: 49 passed
- Python compilation passed for the touched MCP client, registry, compatibility adapter, admin, shell-server, and transport-test modules
- `git diff --check` passed
