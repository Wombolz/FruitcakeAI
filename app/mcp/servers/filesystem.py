"""
FruitcakeAI v5 — Filesystem MCP server (internal_python)

Phase 7.1 starts with a narrow, user-scoped workspace contract:
- list_directory
- find_files
- stat_file
- read_file
- write_file
- make_directory

All file access is constrained to settings.workspace_dir / {user_id}.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


def get_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "list_directory",
            "description": (
                "List files and folders inside the current user's sandbox workspace. "
                "Use this before reading or writing workspace files when the exact path is unknown. "
                "Do not use this for uploaded document-library items; use list_library_documents instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional relative path inside the user's workspace. "
                            "Defaults to the workspace root."
                        ),
                        "default": ".",
                    },
                },
            },
        },
        {
            "name": "find_files",
            "description": (
                "Search for files and folders inside the current user's sandbox workspace by "
                "filename or relative path segment. Use this when the workspace is too "
                "large to inspect one directory at a time. "
                "Do not use this for uploaded document-library items; use list_library_documents instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive filename or path fragment to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional relative directory to search within. Defaults to the workspace root.",
                        "default": ".",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matches to return.",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "stat_file",
            "description": (
                "Inspect metadata for one file or folder in the current user's sandbox workspace. "
                "Use this to check type, size, and modified time before reading or rewriting a path. "
                "Do not use this for uploaded document-library items."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the user's workspace.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "read_file",
            "description": (
                "Read a text file from the current user's sandbox workspace. "
                "Only files inside the user's sandboxed workspace are allowed. "
                "Do not use this to access uploaded document-library files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path inside the user's workspace, for example "
                            "'notes/today.md'. Absolute paths are allowed only when they "
                            "still resolve inside that same workspace."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Write or overwrite a text file in the current user's sandbox workspace. "
                "Creates parent directories when needed. Only paths inside the user's "
                "sandboxed workspace are allowed. "
                "This writes workspace files, not uploaded library documents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the user's workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "make_directory",
            "description": (
                "Create a directory in the current user's sandbox workspace. "
                "Creates parent directories when needed and succeeds if the directory "
                "already exists. Do not use this for uploaded document-library items."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path inside the user's workspace.",
                    },
                },
                "required": ["path"],
            },
        },
    ]


async def call_tool(tool_name: str, arguments: Dict[str, Any], user_context: Any) -> str:
    if tool_name == "list_directory":
        return _list_directory(arguments, user_context)
    if tool_name == "find_files":
        return _find_files(arguments, user_context)
    if tool_name == "stat_file":
        return _stat_file(arguments, user_context)
    if tool_name == "read_file":
        return _read_file(arguments, user_context)
    if tool_name == "write_file":
        return _write_file(arguments, user_context)
    if tool_name == "make_directory":
        return _make_directory(arguments, user_context)
    return f"Unknown filesystem tool: {tool_name}"


def _user_id_from_context(user_context: Any) -> int | None:
    if user_context is None:
        return None
    if isinstance(user_context, dict):
        raw = user_context.get("user_id")
    else:
        raw = getattr(user_context, "user_id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _workspace_root(user_context: Any) -> Path:
    user_id = _user_id_from_context(user_context)
    if user_id is None:
        raise ValueError("Filesystem tools require authenticated user context")
    root = (Path(settings.workspace_dir) / str(user_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_workspace_path(raw_path: str, user_context: Any) -> tuple[Path, Path]:
    workspace_root = _workspace_root(user_context)
    candidate_raw = (raw_path or "").strip()
    if not candidate_raw:
        raise ValueError("Path is required")

    raw = Path(candidate_raw)
    if raw.is_absolute():
        candidate = raw.resolve(strict=False)
    else:
        candidate = (workspace_root / raw).resolve(strict=False)

    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("Path must stay within the user's workspace") from exc

    return workspace_root, candidate


def _read_file(arguments: Dict[str, Any], user_context: Any) -> str:
    _, path = _resolve_workspace_path(arguments.get("path", ""), user_context)
    if not path.exists():
        return f"File not found: {arguments.get('path', '')}"
    if not path.is_file():
        return f"Not a file: {arguments.get('path', '')}"

    size = path.stat().st_size
    if size > settings.filesystem_mcp_max_read_bytes:
        return (
            f"File is too large to read safely ({size} bytes). "
            f"Limit is {settings.filesystem_mcp_max_read_bytes} bytes."
        )

    return path.read_text(encoding="utf-8", errors="replace")


def _list_directory(arguments: Dict[str, Any], user_context: Any) -> str:
    workspace_root, path = _resolve_workspace_path(arguments.get("path", ".") or ".", user_context)
    if not path.exists():
        return f"Directory not found: {arguments.get('path', '.')}"
    if not path.is_dir():
        return f"Not a directory: {arguments.get('path', '.')}"

    entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    rel_dir = path.relative_to(workspace_root)
    display_dir = "." if str(rel_dir) == "." else str(rel_dir)
    if not entries:
        return f"Directory {display_dir} is empty."

    lines = [f"Contents of {display_dir}:"]
    for entry in entries:
        lines.append(_format_entry_line(entry, workspace_root))
    return "\n".join(lines)


def _find_files(arguments: Dict[str, Any], user_context: Any) -> str:
    workspace_root, path = _resolve_workspace_path(arguments.get("path", ".") or ".", user_context)
    if not path.exists():
        return f"Directory not found: {arguments.get('path', '.')}"
    if not path.is_dir():
        return f"Not a directory: {arguments.get('path', '.')}"

    query = (arguments.get("query") or "").strip().lower()
    if not query:
        raise ValueError("Query is required")

    try:
        requested_limit = int(arguments.get("max_results", 20))
    except Exception:
        requested_limit = 20
    limit = max(1, min(requested_limit, settings.filesystem_mcp_max_search_results))

    matches = []
    for entry in path.rglob("*"):
        rel_path = entry.relative_to(workspace_root)
        haystack = str(rel_path).lower()
        if query in haystack:
            matches.append(_format_entry_line(entry, workspace_root))
        if len(matches) >= limit:
            break

    if not matches:
        return f"No files found matching '{arguments.get('query', '')}'."

    rel_dir = path.relative_to(workspace_root)
    display_dir = "." if str(rel_dir) == "." else str(rel_dir)
    lines = [f"Matches for '{arguments.get('query', '')}' in {display_dir}:"]
    lines.extend(matches)
    if len(matches) == limit:
        lines.append(f"(Result limit reached: {limit})")
    return "\n".join(lines)


def _stat_file(arguments: Dict[str, Any], user_context: Any) -> str:
    workspace_root, path = _resolve_workspace_path(arguments.get("path", ""), user_context)
    if not path.exists():
        return f"Path not found: {arguments.get('path', '')}"

    rel_path = path.relative_to(workspace_root)
    stat = path.stat()
    kind = "directory" if path.is_dir() else "file"
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    lines = [
        f"Path: {rel_path}",
        f"Type: {kind}",
        f"Size bytes: {stat.st_size}",
        f"Modified: {modified}",
    ]
    if path.is_file():
        lines.append(f"Extension: {path.suffix or '(none)'}")
    return "\n".join(lines)


def _write_file(arguments: Dict[str, Any], user_context: Any) -> str:
    _, path = _resolve_workspace_path(arguments.get("path", ""), user_context)
    content = arguments.get("content")
    if not isinstance(content, str):
        raise ValueError("Content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > settings.filesystem_mcp_max_write_bytes:
        raise ValueError(
            f"Content is too large to write safely. Limit is {settings.filesystem_mcp_max_write_bytes} bytes."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("Workspace file written", user_id=_user_id_from_context(user_context), path=str(path))
    return f"Wrote {len(encoded)} bytes to {path.name}"


def _make_directory(arguments: Dict[str, Any], user_context: Any) -> str:
    _, path = _resolve_workspace_path(arguments.get("path", ""), user_context)
    if path.exists() and not path.is_dir():
        return f"Cannot create directory because a file already exists at: {arguments.get('path', '')}"
    path.mkdir(parents=True, exist_ok=True)
    log.info("Workspace directory ensured", user_id=_user_id_from_context(user_context), path=str(path))
    return f"Directory ready: {path.name}"


def _format_entry_line(entry: Path, workspace_root: Path) -> str:
    rel_path = entry.relative_to(workspace_root)
    stat = entry.stat()
    kind = "dir" if entry.is_dir() else "file"
    suffix = "/" if entry.is_dir() else ""
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"- [{kind}] {rel_path}{suffix} ({stat.st_size} bytes, modified {modified})"
