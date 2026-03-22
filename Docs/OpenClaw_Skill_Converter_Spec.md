# Phase 7 — Curated OpenClaw Skill Conversion

**Status**: Planned — do not implement until Phase 7 sprint begins
**Related**: `Docs/OpenClaw_Skill_Converter_Spec.md`
**Depends on**: Phase 7 Sprint 7.1 (sandboxed filesystem MCP) and Sprint 7.2
(shell MCP) landing first

---

## Purpose

Phase 7 delivers filesystem and shell MCP tools. When those land, fruitcake's tool
surface becomes rich enough to produce high-quality conversions of the majority of
OpenClaw's 53 bundled skills. This document specifies the additional work needed
in Phase 7 to fully enable the skill converter pipeline.

---

## Tools to Add to Approved List (Sprint 7.x)

These three tools need to be added to the converter's approved tool list and to the
fruitcake tool registry once their MCP servers are live.

### `read_file`
**Source**: Sprint 7.1 sandboxed filesystem MCP
**Description**: Read the contents of a file from the user's sandboxed workspace
(`/workspace/{user_id}/`). Returns raw text content.
**Unlocks**: obsidian, session-logs, active-maintenance, oracle (file path variant),
any skill that reads workspace config or output files.

### `write_file`
**Source**: Sprint 7.1 sandboxed filesystem MCP
**Description**: Write or overwrite a file in the user's sandboxed workspace.
Creates parent directories if needed.
**Unlocks**: obsidian (write paths), session-logs, active-maintenance,
skill-creator, any skill that produces file output.

### `shell_exec`
**Source**: Sprint 7.2 shell MCP (`docker run --network none`, 30s timeout,
8k output cap, blocked commands list)
**Description**: Execute a shell command inside the sandboxed Docker environment.
Returns stdout, stderr, and exit code.
**Unlocks**: github, gog, goplaces, tmux, coding-agent, peekaboo, and any skill
with `bins` dependencies in OpenClaw frontmatter.
**Important**: The converter must only include `shell_exec` in `tool_grants` for
skills where the shell commands are meaningful within the sandbox constraints
(no network, no persistent state outside workspace). Skills that require outbound
network calls via shell (e.g. `curl` to external APIs) should still use
`web_search`/`fetch_page` instead.

## Converter Pipeline Work (Sprint 7.x)

### 7.x.1 — Update converter approved tool list
- Add `read_file`, `write_file`, and `shell_exec` to the approved tool
  list in the converter system prompt.
- Add conditional logic: converter should use Phase 7 tools only when the admin
  specifies `phase7_tools: true` in the convert request. This allows converting
  the same skill twice (Phase 5 version and Phase 7 version) if needed.

### 7.x.2 — Build `/admin/skills/convert` route
- `POST /admin/skills/convert`
- Request body:
```json
  {
    "skill_md": "raw SKILL.md content as string",
    "phase7_tools": true
  }
```
- Calls large model with converter system prompt
- Returns converted JSON payload (does not auto-install)
- Admin reviews payload, then calls `/admin/skills/preview` and
  `/admin/skills/install` to complete install

### 7.x.3 — Batch converter script
- `scripts/convert_openclaw_skills.py`
- Accepts a directory of OpenClaw SKILL.md files
- Calls `/admin/skills/convert` for each
- Writes output JSON files to `scripts/converted_skills/`
- Generates a conversion report:
  - converted successfully
  - skipped (shell-only, no equivalent)
  - needs manual review
- Does not auto-install. Human reviews report before bulk install.

### 7.x.4 — Bulk install flow
- After review, admin calls `/admin/skills/install` for approved payloads
- Track `source: "openclaw"` in skill metadata for provenance
- Log conversion report as admin audit entry

### 7.x.5 — Naming and provenance rules
- Core Fruitcake skills remain first-party Fruitcake skills even when they parallel an OpenClaw concept.
- OpenClaw-derived prefixes or provenance labels should only be applied to direct imported/converted skills.
- Do not brand foundational or product-defining skills as imported just because similar skills existed upstream.

---

## Impact Estimate by Tool

| Tool added | Skills newly unblocked | Notes |
|---|---|---|
| `read_file` + `write_file` | ~40% of 53 bundled | Biggest single unlock |
| `shell_exec` | ~25% additional | github, gog, tmux, coding-agent |
| No new tools (Phase 5 only) | ~25% | summarize, oracle, calendar, RSS skills |

---

## Skills Held for Phase 7 (do not convert before Sprint 7.2)

- `oc-github` — requires `gh` and `git` via shell_exec
- `oc-gog` — requires gog binary
- `oc-tmux` — requires tmux binary
- `oc-coding-agent` — requires shell-heavy workflows outside current curated scope
- `oc-peekaboo` — macOS UI automation binary
- `oc-skill-creator` — remains deferred because direct import would blur the line between core Fruitcake skills and imported skills
- `oc-obsidian` (write paths) — requires write_file
- `oc-node-connect` — requires Node.js binary
- `oc-openai-whisper` — requires whisper binary
- `oc-sherpa-onnx-tts` — requires TTS binary
- Any skill with non-empty `bins` list in OpenClaw frontmatter

---

## Acceptance Criteria for This Sprint

1. `/admin/skills/convert` route accepts SKILL.md and returns valid JSON payload.
2. Converter correctly omits Phase 7 tools when `phase7_tools: false`.
3. Batch script processes a directory of SKILL.md files and produces a conversion
   report without auto-installing.
4. `shell_exec` tool grants are only included for skills where sandbox constraints
   are respected (no outbound network via shell).
5. Converted skills are selected for product fit, not just technical convertibility.
6. All converted skills pass the existing `/admin/skills/preview` validation.
7. Conversion report distinguishes: converted, skipped, needs-review.
