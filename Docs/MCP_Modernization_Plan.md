# MCP Modernization Plan For Fruitcake (Full Hardening)

## Summary
Modernize MCP reliability and config ergonomics while keeping your current broad tool set enabled.  
Use a **hybrid search strategy with internal `web_research` as primary**, and keep DuckDuckGo MCP as secondary/fallback.  
Borrow proven behavior from external MCP where useful, but keep your internal server as the stable contract surface for the agent.

## Current-State Findings (From Repo)
- `config/mcp_config.yaml` currently enables many Docker MCP servers and has `web_research` disabled.
- MCP registry currently supports only:
  - `internal_python` with `module`
  - `docker_stdio` with `image` and `timeout`
- Registry does not currently consume `priority` for tool selection.
- Docker server launch is fixed as `docker run -i --rm <image>` with no per-server args/env/volumes.
- MCP client had early-protocol assumptions; recent fixes improved parsing and response-id matching.
- There is still no explicit per-client request serialization lock, and no auto-reconnect flow when a docker MCP process dies mid-runtime.
- Tool-name collision handling is implicit “last write wins” in `_tool_map` (no warning/error policy).

## Decisions Locked
- Scope: **Full hardening**
- Default profile: **Keep current broad set enabled**
- Search strategy: **Hybrid; internal `web_research` primary**
- Web fallback strategy: **Compare and borrow from DuckDuckGo MCP; retain internal server as primary surface**

## Implementation Plan

## 1. Define a Stable MCP Config Schema (Backward-Compatible)
- Extend `config/mcp_config.yaml` server schema with optional fields:
  - `transport`: `docker_stdio | sse | internal_python`
  - `command`, `args`, `env`, `volumes`, `network`, `extra_docker_args`
  - `healthcheck` settings: interval, timeout, retry budget
  - `restart_policy`: `never | on_failure`
  - `tool_prefix` and `tool_aliases`
- Keep old keys (`type`, `image`, `timeout`) working.
- Add schema validation at startup with actionable errors per server entry.

## 2. Harden MCP Client Runtime Reliability
- Add per-client async request lock so only one stdio request/response exchange is active per client at a time.
- Keep id-based response matching and framed parsing.
- Add reconnect behavior:
  - On broken pipe/EOF/timeout: mark disconnected, attempt reconnect once, re-discover tools, retry call once.
- Capture stderr ring buffer for docker MCP clients and expose recent lines for diagnostics.
- Add server-call metrics:
  - call count, success/failure, timeout count, reconnect count, p95 latency.

## 3. Harden Registry Behavior
- Add deterministic collision policy:
  - Detect duplicate tool names across servers at startup.
  - Default policy: keep first registered and log structured warning.
  - Support explicit aliasing/prefix to avoid ambiguity.
- Add optional server-level tool filtering in config:
  - `allow_tools`, `block_tools`, `rename_tools`.
- Add health status model in registry:
  - `connected`, `last_error`, `last_ok_at`, `consecutive_failures`.

## 4. Improve Admin Observability
- Expand `/admin/tools` output with:
  - per-server transport/config summary
  - connection and health state
  - collision/alias info
  - last error + stderr snippet
- Add `/admin/mcp/diagnostics` endpoint for targeted runtime checks:
  - ping server
  - list tools
  - single dry-run call (configurable tool/args)

## 5. Search Strategy Refactor (Hybrid, Internal Primary)
- Re-enable internal `web_research` and make it the agent-facing stable search/fetch interface.
- Keep DuckDuckGo MCP enabled as secondary provider.
- Add internal search provider routing:
  - primary: internal implementation
  - secondary: external DuckDuckGo MCP if primary fails/timeout
- Keep agent-visible tool names stable (`web_search`, `fetch_page`) to reduce planner drift.
- Borrow from DuckDuckGo MCP where useful:
  - result normalization
  - richer snippets
  - query handling patterns
- Do not expose raw external generic tool names like `search` as primary contract.

## 6. Config Refresh (Broad Set, Safer Defaults)
- Keep current broad enabled set per your preference.
- Add explicit comments and safety notes for each server.
- For high-risk servers (`shell`, `git`, `postgres`) when added later:
  - require explicit enable flags
  - recommend read-only mode where applicable
  - document persona-level tool blocking defaults.

## 7. Rollout Plan
- Phase A: observability + non-breaking reliability changes.
- Phase B: config schema expansion + collision controls.
- Phase C: internal-primary hybrid search routing and fallback.
- Phase D: production soak with failure-injection tests and tightened alerts.

## Public Interfaces / Contract Changes
- `config/mcp_config.yaml`:
  - additive fields for transport/runtime options and collision handling.
- `/admin/tools` response:
  - additive health/diagnostic fields.
- New endpoint:
  - `/admin/mcp/diagnostics` (admin only).
- Agent tool contract:
  - stable preference toward `web_search`/`fetch_page` from internal server; external aliases hidden or secondary.

## Test Plan

## Backend Automated
- MCP client:
  - ignores notifications and waits for matching response id
  - handles Content-Length and multi-line JSON frames
  - serializes concurrent calls via lock
  - reconnect-and-retry once on EOF/broken pipe
- Registry:
  - collision detection and policy enforcement
  - alias mapping correctness
  - per-server status state transitions
- Search routing:
  - primary success path (internal)
  - primary failure -> secondary success
  - both failure -> deterministic error message

## Integration / E2E
- Chat-mode repeated web searches from multiple sessions concurrently.
- Task-run mode and chat-mode both produce consistent search reliability.
- `/admin/tools` and diagnostics reflect real-time failures/recovery.
- Restart behavior:
  - tools reconnect
  - no stale connected=true when process is dead.

## Acceptance Criteria
- No intermittent “No response from server” under concurrent chat traffic in staging load test.
- Search behavior stable across chat and task contexts.
- Duplicate tool names do not silently override without visibility.
- Admin can identify failing MCP server cause without reading app logs directly.

## Assumptions And Defaults
- Keep broad MCP stack enabled by default (your choice).
- Internal `web_research` becomes primary search contract.
- DuckDuckGo MCP remains enabled as secondary provider.
- Existing task/chat APIs stay backward compatible; all interface changes are additive.
