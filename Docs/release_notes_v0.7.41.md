# Release Notes v0.7.41

## Summary

This release turns Fruitcake's rich chat content into an extensible artifact platform and adds the first portable MCP Apps host. Internal tools and connected MCP servers can return compact model-facing text alongside validated UI data, safe static HTML or SVG, or a sandboxed interactive app. The Stocks companion serves as the first end-to-end reference without becoming a required Fruitcake dependency.

## Included Changes

- added a bounded, versioned artifact envelope and renderer registry
- retained compatibility with existing table, chart, timeline, place, file, code, news, and metric content blocks
- added sanitized `core.html` and `core.svg` artifacts with JavaScript and remote-resource restrictions
- added a host-owned artifact creation tool for model-requested static HTML and SVG displays
- added MCP Apps capability advertisement, linked `ui://` resource discovery, resource validation, and sandboxed rendering
- added durable `core.mcp_app` metadata with compact text fallback and provenance
- kept app-only tools out of model tool catalogs
- allowed audited, schema-valid, same-server read-only app tool calls for trusted first-party extensions
- added the optional Stocks companion configuration as the first interactive reference app
- added graceful Ollama model unloading during server shutdown

## Security And Compatibility

- no database migration is required
- unknown artifact types retain their readable fallback instead of breaking chat
- MCP App resources must be explicitly linked by a registered tool and use the expected media type
- app-originated mutations remain blocked pending a dedicated approval-aware implementation
- app views receive no Fruitcake credentials, direct filesystem access, or undeclared network access
- hosts without MCP Apps support can continue using ordinary MCP tool text and structured results
- local-model release affects only Ollama models used by the current Fruitcake process and can be disabled with `LOCAL_MODEL_RELEASE_ON_SHUTDOWN=false`

## Verification

- artifact, MCP, transport, chat persistence, agent, memory extraction, and local-model lifecycle coverage passed
- Python compile checks, shell syntax validation, and `git diff --check` passed before release preparation
