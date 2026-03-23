# Sprint 7.1 — Sandboxed Filesystem MCP

**Status**: In progress  
**Phase**: 7 — Trusted Local Capability Expansion  
**Depends on**: existing internal MCP registry and user-scoped `UserContext`

---

## Summary

Add a safe, user-scoped filesystem MCP server so Fruitcake can read and write files in a per-user workspace without breaking the local-first trust model.

This sprint is about a narrow, trustworthy workspace contract, not broad filesystem power.

Initial tool surface:
- `list_directory`
- `find_files`
- `stat_file`
- `read_file`
- `write_file`
- `make_directory`

Workspace rule:
- all filesystem access is constrained to `workspace/{user_id}/`
- tool calls derive `user_id` from authenticated `user_context`, never from tool arguments

---

## Goals

1. Give the agent a real per-user workspace it can inspect and use.
2. Keep file access legible, auditable, and bounded.
3. Establish the filesystem seam needed for later curated skill conversion.
4. Preserve Fruitcake's local-first positioning by making the workspace safe by default.

---

## Locked Decisions

- Implement as an **internal Python MCP server** first, not a Docker-first server.
- Enforce path safety on the server side using authenticated user context.
- Keep the first slice limited to discovery and text file read/write.
- Do not broaden this sprint into shell execution, general OS file access, or cross-user workspaces.

---

## Scope

### In scope
- workspace root config
- per-user workspace path resolution
- `list_directory`
- `find_files`
- `stat_file`
- `read_file`
- `write_file`
- `make_directory`
- path traversal prevention
- basic read/write byte caps
- MCP registry wiring
- tests for path safety and tool registration

### Not in scope
- `shell_exec`
- delete, move, rename, or copy tools
- recursive search/indexing tools
- binary file handling
- cross-user sharing via filesystem paths
- direct converter expansion work

---

## Contract

### Workspace layout
- base root comes from config: `workspace_dir`
- each user gets:
  - `workspace/{user_id}/`

### Tool behavior

#### `list_directory`
- lists files and folders inside the current user's workspace
- accepts an optional relative path
- refuses paths outside the workspace

#### `read_file`
- reads a text file inside the current user's workspace
- returns text content
- refuses missing paths, directories, oversized files, and out-of-bounds paths

#### `find_files`
- searches file and folder names recursively inside the current user's workspace
- supports optional directory scoping
- returns bounded filename/path matches only
- does not inspect file contents

#### `stat_file`
- returns metadata for one file or folder
- exposes type, size, modified time, and file extension when applicable
- gives the agent a safer inspection step before reading or overwriting content

#### `make_directory`
- creates a directory path inside the user's workspace
- creates parent directories when needed
- succeeds if the directory already exists
- refuses paths outside the workspace or paths that collide with existing files

#### `write_file`
- writes UTF-8 text to a file inside the current user's workspace
- creates parent directories as needed
- refuses out-of-bounds paths and oversized writes

---

## Guardrails

- No tool may escape the resolved workspace root.
- Absolute paths are allowed only if they still resolve inside the user's workspace.
- Tool arguments must not be trusted for user identity.
- Reads and writes are capped to avoid dumping arbitrary file sizes into model context.
- The sprint should remain useful without weakening auditability or user trust.

---

## Why This Phase Comes Before Shell

Filesystem MCP is the lower-risk foundation:
- it adds practical local capability
- it stays understandable to users
- it supports curated skill conversion later
- it does not yet introduce shell complexity, binary dependencies, or execution sprawl

This is the right first step for Phase 7.

---

## Acceptance Criteria

1. MCP registry exposes `list_directory`, `find_files`, `stat_file`, `read_file`, `write_file`, and `make_directory`.
2. Files are always resolved under `workspace/{user_id}/`.
3. Path traversal and cross-user path access are blocked.
4. The agent can discover files before attempting reads.
5. Reads and writes respect configured byte caps.
6. Targeted filesystem and MCP tests pass.

---

## Follow-on Work

Likely next additions after this sprint proves out:
- selective directory search helpers
- better chat/task evaluations for workspace tool usage
- Phase 7.2 shell MCP
- Phase 7.4 curated converter updates that rely on `read_file` and `write_file`

Do not treat this sprint as license to add broad local automation by default.
