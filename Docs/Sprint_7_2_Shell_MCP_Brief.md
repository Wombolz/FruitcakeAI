# Sprint 7.2 — Sandboxed Shell MCP

**Status**: In progress  
**Phase**: 7 — Trusted Local Capability Expansion  
**Depends on**: Phase 7.1 workspace filesystem MCP and existing MCP stdio runtime

---

## Summary

Add a tightly sandboxed shell MCP path so Fruitcake can run selected local CLI workflows without turning into a general-purpose automation lab.

This sprint is not about “arbitrary shell access.” It is about a narrow, inspectable execution contract with explicit sandbox flags and product guardrails.

Initial tool surface:
- `shell_exec`

Execution rule:
- shell commands run inside a sandboxed container
- no outbound network
- workspace-only writable mount
- bounded runtime and output

---

## Goals

1. Enable a small class of local shell-backed workflows that are still on-brand for Fruitcake.
2. Keep shell execution clearly below the filesystem trust bar and well above “run anything.”
3. Establish the shell contract needed for later curated skill conversion.
4. Preserve the local-first positioning without slipping into high-risk generic automation.

---

## Locked Decisions

- Implement via **docker stdio MCP**, not direct host shell execution.
- Keep network disabled: `docker run --network none`.
- Keep the writable mount limited to the per-user workspace.
- Enforce bounded runtime and bounded stdout/stderr.
- Maintain an explicit blocked-command policy for obviously unsafe or identity-breaking commands.
- Do not broaden this sprint into package installation, host-level admin actions, or unrestricted developer shell tooling.

---

## Scope

### In scope
- shell MCP sprint brief and runtime contract
- docker stdio config support for custom run args
- shell server registration path in MCP config
- Fruitcake-owned standalone shell MCP server implementation
- timeout and output-cap expectations
- blocked-command policy definition
- tests for config-driven docker stdio arguments
- end-to-end Docker-backed shell MCP smoke coverage

### Not in scope
- direct host shell execution
- privileged containers
- network-enabled shell
- package-manager/bootstrap flows
- shell-driven browser/network scraping
- blanket approval of shell-heavy imported skills

---

## Contract

### Runtime model
- `shell_exec` runs inside a sandboxed Docker container through the MCP stdio path
- expected docker flags:
  - `--rm`
  - `-i`
  - `--network none`
  - workspace bind mount only
- no host root mount
- no access outside the mounted workspace contract

### Tool behavior

#### `shell_exec`
- accepts a command string
- returns:
  - stdout
  - stderr
  - exit code
- enforces:
  - timeout
  - output cap
  - blocked-command rejection before execution when possible

### Standalone distribution note
- the shell server is implemented as a small Fruitcake-owned standalone-ready MCP component under `mcp_shell_server/`
- Fruitcake consumes it first through Docker stdio, but the server should remain cleanly extractable for separate build/publish later
- current local image contract:
  - build: `docker build -t fruitcake/mcp-shell -f mcp_shell_server/Dockerfile .`
  - run entrypoint: `python -m mcp_shell_server.server`
- if this server is later published independently, keep the CLI/config surface stable:
  - `--allowed-paths`
  - `--timeout-seconds`
  - `--output-limit-bytes`
  - blocked-policy file support

### Safety posture
- shell is more dangerous than filesystem tools and must stay visibly constrained
- this sprint should prefer “refuse clearly” over “try anyway”
- technical ability to run a command is not enough to make it acceptable product behavior

---

## Guardrails

- no outbound network from the shell container
- workspace-only writable mount
- explicit blocked commands list for obviously unsafe operations
- no shell command should mutate outside the mounted workspace
- shell usage should remain auditable through the normal MCP/audit path
- later converter work must treat `shell_exec` as selective, not default

---

## Why This Sprint Exists

Phase 7.1 made the workspace legible.

Phase 7.2 adds a controlled execution layer on top of that workspace so Fruitcake can support a limited class of useful local workflows, but without taking on the identity of a generic coding or ops agent.

This is useful only if the sandbox contract is explicit and narrow.

---

## Acceptance Criteria

1. MCP docker stdio runtime supports config-defined docker run arguments cleanly.
2. A Fruitcake-owned shell MCP server exists and can be configured with:
   - no network
   - workspace-only mount
   - timeout
3. Runtime/config tests prove the shell server flags are passed through as intended.
4. Server behavior tests prove:
   - allowed workspace command execution
   - blocked-command refusal
   - timeout handling
   - output truncation
5. A real Docker-backed smoke test works through Fruitcake's MCP registry.
6. The sprint brief and config make the shell trust boundary explicit.
7. Shell capability remains additive and constrained; no host-shell shortcut is introduced.

---

## Follow-on Work

Likely next additions after the shell contract proves out:
- shell-specific admin diagnostics
- converter updates that selectively allow `shell_exec`
- optional default enablement after soak and product review

Do not treat this sprint as a green light for broad shell-first product behavior.
