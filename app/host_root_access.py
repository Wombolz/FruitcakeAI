from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autonomy.approval import ApprovalRequired
from app.config import settings
from app.db.models import ApprovedHostRoot

_BROAD_ROOT_HINTS = {
    "/",
    "/Users",
    "/private",
    "/tmp",
    "/var",
    "/Volumes",
}
_REPO_TREE_SKIP_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}
_HIGH_VALUE_ROOTS = ("app", "tests", "config", "scripts", "Docs")
_NOISE_ROOTS = (".claude", ".tmp_pdf_review_env", "node_modules", "dist", "build")
_TEXT_SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".ini",
    ".cfg",
    ".sh",
}
_MARKER_TOKENS = ("TODO", "FIXME", "XXX")
_TEXT_PREVIEW_FILENAMES = (
    "README.md",
    "README.txt",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "AGENTS.md",
    "SKILL.md",
)
_ENTRYPOINT_CANDIDATES = {
    "overview": ("README.md", "requirements.txt"),
    "agent_runtime": (
        "app/agent/core.py",
        "app/agent/chat_orchestration.py",
        "app/agent/definition_loader.py",
        "app/agent/persona_loader.py",
    ),
    "autonomy": (
        "app/autonomy/runner.py",
        "app/autonomy/profiles/repo_map.py",
        "app/autonomy/approval.py",
    ),
    "api": (
        "app/api/chat.py",
        "app/api/tasks.py",
        "app/api/mcp_server.py",
        "app/api/admin.py",
    ),
    "data_model": (
        "app/db/models.py",
        "app/db/session.py",
        "app/db/migrations/",
    ),
    "config": (
        "config/agents.yaml",
        "config/mcp_config.yaml",
    ),
    "tests": (
        "tests/test_task_steps.py",
        "tests/test_mcp_server_api.py",
    ),
}


def _workspace_root_for_user(user_id: int) -> Path:
    return (Path(settings.workspace_dir) / str(int(user_id))).resolve()


def _resolve_candidate_path(raw_path: str) -> Path:
    candidate = Path(str(raw_path or "").strip()).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Host-root access requests must use absolute paths.")
    return candidate.resolve(strict=False)


def _is_within_path(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def is_workspace_path_for_user(user_id: int, raw_path: str) -> bool:
    candidate = _resolve_candidate_path(raw_path)
    return _is_within_path(candidate, _workspace_root_for_user(user_id))


def validate_host_root_candidate(raw_path: str) -> Path:
    candidate = _resolve_candidate_path(raw_path)
    normalized = str(candidate)
    if normalized in _BROAD_ROOT_HINTS:
        raise ValueError("Requested host root is too broad to approve.")

    home = Path.home().resolve(strict=False)
    if candidate == home or candidate == home.parent:
        raise ValueError("Requested host root is too broad to approve.")

    if any(part in {"*", "?"} for part in candidate.parts):
        raise ValueError("Wildcard host roots are not allowed.")

    if len(candidate.parts) < 4:
        raise ValueError("Requested host root is too broad to approve.")

    return candidate


async def is_host_root_approved(
    db: AsyncSession,
    *,
    user_id: int,
    raw_path: str,
    access_mode: str = "read_only",
) -> bool:
    candidate = validate_host_root_candidate(raw_path)
    rows = await db.execute(
        select(ApprovedHostRoot).where(
            ApprovedHostRoot.user_id == int(user_id),
            ApprovedHostRoot.access_mode == str(access_mode or "read_only"),
            ApprovedHostRoot.is_active.is_(True),
        )
    )
    for row in rows.scalars().all():
        root = Path(str(row.canonical_path or "")).resolve(strict=False)
        if _is_within_path(candidate, root):
            return True
    return False


async def ensure_host_root_access(
    db: AsyncSession,
    *,
    user_id: int,
    raw_path: str,
    requester: str,
    reason: str,
    access_mode: str = "read_only",
) -> Path:
    candidate = validate_host_root_candidate(raw_path)
    if await is_host_root_approved(
        db,
        user_id=user_id,
        raw_path=str(candidate),
        access_mode=access_mode,
    ):
        return candidate

    raise ApprovalRequired(
        "host_root_access",
        reason,
        approval_kind="host_root_access",
        payload={
            "requested_path": str(candidate),
            "requested_access_mode": str(access_mode or "read_only"),
            "requester": str(requester or "").strip() or "unknown",
            "reason": str(reason or "").strip(),
        },
    )


async def activate_host_root_grant(
    db: AsyncSession,
    *,
    user_id: int,
    raw_path: str,
    created_by_user_id: int | None,
    access_mode: str = "read_only",
    approval_source: str = "task_approval",
) -> ApprovedHostRoot:
    candidate = validate_host_root_candidate(raw_path)
    row = (
        await db.execute(
            select(ApprovedHostRoot).where(
                ApprovedHostRoot.user_id == int(user_id),
                ApprovedHostRoot.canonical_path == str(candidate),
                ApprovedHostRoot.access_mode == str(access_mode or "read_only"),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ApprovedHostRoot(
            user_id=int(user_id),
            canonical_path=str(candidate),
            access_mode=str(access_mode or "read_only"),
            is_active=True,
            created_by_user_id=int(created_by_user_id) if created_by_user_id is not None else None,
            approval_source=str(approval_source or "task_approval"),
        )
        db.add(row)
        await db.flush()
        return row

    row.is_active = True
    row.created_by_user_id = int(created_by_user_id) if created_by_user_id is not None else row.created_by_user_id
    row.approval_source = str(approval_source or row.approval_source or "task_approval")
    await db.flush()
    return row


def build_host_root_scan_summary(
    root: Path,
    *,
    ignored_paths: Iterable[str] | None = None,
    max_depth: int = 3,
    max_entries: int = 180,
    preview_char_limit: int = 1200,
) -> dict[str, Any]:
    ignored = {str(item).strip().strip("/") for item in (ignored_paths or []) if str(item).strip()}
    tree_lines: list[str] = []
    seen_entries = 0

    def _walk(current: Path, depth: int) -> None:
        nonlocal seen_entries
        if seen_entries >= max_entries or depth > max_depth:
            return
        try:
            children = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except Exception:
            return
        for child in children:
            if seen_entries >= max_entries:
                return
            if child.is_symlink():
                continue
            if child.name in _REPO_TREE_SKIP_NAMES:
                continue
            relative = child.relative_to(root).as_posix()
            if relative in ignored or any(relative.startswith(f"{prefix}/") for prefix in ignored):
                continue
            prefix = "  " * depth
            marker = "/" if child.is_dir() else ""
            tree_lines.append(f"{prefix}{relative}{marker}")
            seen_entries += 1
            # Surface noisy top-level roots without letting them dominate the preview.
            if child.is_dir() and not (depth == 0 and child.name in _NOISE_ROOTS):
                _walk(child, depth + 1)

    _walk(root, 0)
    top_level_names = _visible_top_level_names(root)
    key_roots = _collect_key_roots(root, ignored_paths=ignored, top_level_names=top_level_names)
    entrypoints = _collect_entrypoints(root)
    marker_hits = _collect_marker_hits(root, ignored_paths=ignored)
    secondary_context = _collect_secondary_context(root, top_level_names=top_level_names)
    noise_areas = [
        {
            "path": name,
            "source_class": "observed_secondary",
            "note": "Secondary context only; do not treat as repo-canonical structure.",
        }
        for name in _NOISE_ROOTS
        if name in top_level_names
    ]

    preview_file = None
    preview_text = ""
    for filename in _TEXT_PREVIEW_FILENAMES:
        candidate = root / filename
        if not candidate.exists() or not candidate.is_file():
            continue
        if candidate.is_symlink():
            continue
        try:
            preview_text = candidate.read_text(encoding="utf-8", errors="replace")[:preview_char_limit].strip()
        except Exception:
            preview_text = ""
        preview_file = candidate.name
        if preview_text:
            break

    return {
        "tree_lines": tree_lines,
        "key_roots": key_roots,
        "entrypoints": entrypoints,
        "marker_hits": marker_hits,
        "secondary_context": secondary_context,
        "noise_areas": noise_areas,
        "preview_file": preview_file,
        "preview_text": preview_text,
    }


def _visible_top_level_names(root: Path) -> list[str]:
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except Exception:
        return []
    names: list[str] = []
    for child in children:
        if child.is_symlink():
            continue
        if child.name in _REPO_TREE_SKIP_NAMES:
            continue
        names.append(child.name)
    return names


def _collect_key_roots(
    root: Path,
    *,
    ignored_paths: set[str],
    top_level_names: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in _HIGH_VALUE_ROOTS:
        if name not in top_level_names:
            continue
        candidate = root / name
        kind = "directory" if candidate.is_dir() else "file"
        highlights: list[str] = []
        if candidate.is_dir():
            try:
                children = sorted(candidate.iterdir(), key=lambda item: item.name.lower())
            except Exception:
                children = []
            for child in children:
                if child.is_symlink():
                    continue
                relative = child.relative_to(root).as_posix()
                if child.name in _REPO_TREE_SKIP_NAMES:
                    continue
                if relative in ignored_paths or any(relative.startswith(f"{prefix}/") for prefix in ignored_paths):
                    continue
                marker = "/" if child.is_dir() else ""
                highlights.append(f"{relative}{marker}")
                if len(highlights) >= 6:
                    break
        results.append(
            {
                "path": f"{name}/" if candidate.is_dir() else name,
                "kind": kind,
                "source_class": "observed_top_level",
                "highlights": highlights,
            }
        )
    return results


def _collect_entrypoints(root: Path) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for subsystem, candidates in _ENTRYPOINT_CANDIDATES.items():
        for relative in candidates:
            candidate = root / relative
            if candidate.exists():
                results.append(
                    {
                        "subsystem": subsystem,
                        "path": relative,
                        "source_class": "observed_top_level",
                    }
                )
                break
    return results


def _collect_secondary_context(root: Path, *, top_level_names: list[str]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    if ".claude" in top_level_names:
        worktrees_dir = root / ".claude" / "worktrees"
        note = "Secondary local worktree context; not repo-canonical unless explicitly configured."
        if worktrees_dir.exists() and worktrees_dir.is_dir():
            results.append(
                {
                    "path": ".claude/worktrees/",
                    "source_class": "observed_secondary",
                    "note": note,
                }
            )
        else:
            results.append(
                {
                    "path": ".claude/",
                    "source_class": "observed_secondary",
                    "note": note,
                }
            )
    if ".tmp_pdf_review_env" in top_level_names:
        results.append(
            {
                "path": ".tmp_pdf_review_env/",
                "source_class": "observed_secondary",
                "note": "Bundled environment snapshot; treat as support/noise, not a primary code root.",
            }
        )
    return results


def _collect_marker_hits(
    root: Path,
    *,
    ignored_paths: set[str],
    max_hits: int = 12,
    max_files: int = 240,
    max_bytes: int = 200_000,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    files_checked = 0

    search_roots = [root / name for name in _HIGH_VALUE_ROOTS if (root / name).exists()]
    if not search_roots:
        search_roots = [root]

    for start in search_roots:
        if len(hits) >= max_hits or files_checked >= max_files:
            break
        for path in sorted(start.rglob("*")):
            if len(hits) >= max_hits or files_checked >= max_files:
                break
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if any(part in _REPO_TREE_SKIP_NAMES for part in path.relative_to(root).parts):
                continue
            if relative in ignored_paths or any(relative.startswith(f"{prefix}/") for prefix in ignored_paths):
                continue
            if path.suffix.lower() not in _TEXT_SCAN_SUFFIXES and path.name not in _TEXT_PREVIEW_FILENAMES:
                continue
            try:
                size = path.stat().st_size
            except Exception:
                continue
            if size > max_bytes:
                continue
            files_checked += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line_no, raw_line in enumerate(text.splitlines(), start=1):
                marker = next((token for token in _MARKER_TOKENS if token in raw_line), None)
                if not marker:
                    continue
                hits.append(
                    {
                        "path": relative,
                        "line": line_no,
                        "marker": marker,
                        "snippet": raw_line.strip()[:180],
                    }
                )
                if len(hits) >= max_hits:
                    break
    return hits
