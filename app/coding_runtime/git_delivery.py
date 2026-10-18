"""Reviewed, fixed-form local Git operations. No shell, force, stash, or push."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

from app.coding_runtime.repository import RepositoryError


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def commit_plan(manager, root, checkout, files, message):
    if not message.strip() or len(message.encode()) > 4096 or "\0" in message:
        raise RepositoryError("invalid_request", "A commit message of 1-4096 UTF-8 bytes is required")
    if not files or len(files) > 200 or len(set(files)) != len(files):
        raise RepositoryError("invalid_request", "Select 1-200 distinct changed files")
    changed = {item.path for item in await manager._files(root)}
    total_bytes = 0
    for path in files:
        if path not in changed:
            raise RepositoryError("repository_changed", "Selected files changed. Review a new commit preview.")
        manager._validate_file(root, path)
        if (root / path).exists():
            total_bytes += (root / path).stat().st_size
    if total_bytes > 1024*1024:
        raise RepositoryError("repository_limit", "Select at most 1 MiB of text files per reviewed commit")
    # Never consume or discard an operator's pre-existing staging choices.
    staged, _ = await manager._git(root, "diff", "--cached", "--name-only", "-z")
    if staged:
        raise RepositoryError("checkout_conflict", "This checkout has staged changes. Resolve its index in an external Git client first.")
    head = checkout.head_commit or checkout.baseline_commit
    with tempfile.TemporaryDirectory(prefix="fruitcake-commit-index-") as temporary:
        index = str(Path(temporary) / "index")
        await manager._git(root, "read-tree", head, _index=index)
        await manager._git(root, "add", "--", *sorted(files), _index=index)
        modes, _ = await manager._git(root, "ls-files", "--stage", "-z", "--", *sorted(files), _index=index)
        if any(record.split(b" ", 1)[0] not in {b"100644", b"100755"} for record in modes.split(b"\0") if record):
            raise RepositoryError("unsupported_file", "Only regular file additions, changes, and deletions can be committed")
        tree, _ = await manager._git(root, "write-tree", _index=index)
        diff, truncated = await manager._git(root, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color",
                                             "--no-renames", "--binary", head, "--", _index=index, limit=2*1024*1024)
    if not diff or truncated or b"GIT binary patch" in diff:
        raise RepositoryError("unsupported_file", "Select changed text files within the 2 MiB review limit")
    plan = {"action": "commit", "checkout_id": checkout.id, "head": head,
            "branch": checkout.branch_name or f"fruitcake/{checkout.id}", "files": sorted(files),
            "message": message, "tree": tree.decode().strip(), "diff": diff.decode("utf-8", errors="strict")}
    return {**plan, "review_hash": digest(plan)}


async def create_commit(manager, root, plan):
    branch = f"refs/heads/{plan['branch']}"
    ref, _ = await manager._git(root, "rev-parse", "--verify", branch, allowed=(0, 128))
    if ref and ref.decode().strip() != plan["head"]:
        raise RepositoryError("repository_changed", "The managed branch changed; nothing was committed")
    commit, _ = await manager._git(root, "-c", "user.name=Fruitcake", "-c", "user.email=fruitcake@localhost",
                                  "-c", "commit.gpgSign=false", "commit-tree", plan["tree"], "-p", plan["head"], "-m", plan["message"])
    commit = commit.decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise RepositoryError("checkout_invalid", "Git did not return a valid commit identity")
    await manager._git(root, "update-ref", branch, commit, ref.decode().strip() if ref else "0" * len(commit))
    await manager._git(root, "symbolic-ref", "HEAD", branch)
    await manager._git(root, "read-tree", commit)
    return commit


async def source_plan(manager, source, checkout):
    await manager._validate_root(source)
    branch, _ = await manager._git(source, "symbolic-ref", "-q", "HEAD", allowed=(0, 1))
    if not branch:
        raise RepositoryError("checkout_conflict", "The source repository needs a checked-out local branch")
    dirty, _ = await manager._git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    if dirty:
        raise RepositoryError("checkout_dirty", "The source contains changed, untracked, or ignored files. Nothing will be overwritten or stashed.")
    source_head = await manager.baseline(source)
    head = checkout.head_commit or checkout.baseline_commit
    if source_head == head:
        raise RepositoryError("checkout_conflict", "These commits are already present in the source")
    base, _ = await manager._git(source, "merge-base", source_head, head, allowed=(0, 1))
    if base.decode().strip() != source_head:
        raise RepositoryError("checkout_conflict", "Source history has diverged. Resolve it in an external Git client; automatic merging is unavailable.")
    diff, _ = await manager._git(source, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                                  source_head, head, "--", limit=2*1024*1024)
    plan = {"action": "apply", "checkout_id": checkout.id, "head": head, "source_head": source_head,
            "source_branch": branch.decode().strip(), "source_path": str(source),
            "diff": diff.decode("utf-8", errors="strict")}
    return {**plan, "review_hash": digest(plan)}


async def apply_source(manager, source, plan):
    await manager._git(source, "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", plan["head"])
    if await manager.baseline(source) != plan["head"]:
        raise RepositoryError("repository_changed", "Source advancement could not be verified; inspect the repository")
