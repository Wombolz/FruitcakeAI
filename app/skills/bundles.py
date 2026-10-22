"""Bounded text-only skill bundles. Client paths never become server paths."""
from __future__ import annotations

import hashlib
import json
import posixpath
import re

MAX_FILES = 32
MAX_FILE_BYTES = 64_000
MAX_BUNDLE_BYTES = 512_000
MAX_MAIN_CHARS = 12_000


def validate_files(files: dict[str, str]) -> dict[str, str]:
    if not files or len(files) > MAX_FILES or "SKILL.md" not in files:
        raise ValueError("Choose a bundle containing SKILL.md and at most 32 text files")
    total = 0
    result = {}
    for path, content in files.items():
        if (
            not isinstance(path, str) or not 1 <= len(path) <= 240
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or "\\" in path or ":" in path or any(ord(c) < 32 for c in path)
            or not path.lower().endswith((".md", ".txt"))
            or not isinstance(content, str) or "\x00" in content
            or any(ord(c) < 32 and c not in "\n\r\t" for c in content)
        ):
            raise ValueError("Bundle files must be relative Markdown/text paths without traversal")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError("A skill file exceeds the 64 KB limit")
        total += size
        result[path] = content
    if total > MAX_BUNDLE_BYTES or len(result["SKILL.md"]) > MAX_MAIN_CHARS:
        raise ValueError("Bundle exceeds 512 KB or SKILL.md exceeds 12,000 characters")
    # A frozen bundle must not leave local documentation links dangling.
    for path, content in result.items():
        for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", content):
            target = target.split("#", 1)[0]
            if not target or ":" in target:
                continue
            if target.startswith("/") or "\\" in target:
                raise ValueError("Skill documentation links must stay inside the bundle")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if resolved == ".." or resolved.startswith("../"):
                raise ValueError("Skill documentation links must stay inside the bundle")
            if target.lower().endswith((".md", ".txt")) and resolved not in result:
                raise ValueError(f"Missing referenced file: {resolved}")
    return dict(sorted(result.items()))


def bundle_hash(files: dict[str, str], scope: str, personal_user_id: int | None, pinned: bool) -> str:
    payload = {"files": files, "scope": scope, "personal_user_id": personal_user_id, "pinned": pinned}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
