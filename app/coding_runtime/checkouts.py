"""Fruitcake-owned Git worktrees with immutable baselines and tracked HEADs."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import stat

from app.coding_runtime.repository import RepositoryError, RepositoryInspector


class CheckoutManager(RepositoryInspector):
    def __init__(self, directory: Path):
        super().__init__(timeout=60)
        self.directory = directory.absolute()

    def path(self, user_id: int, checkout_id: str) -> Path:
        if user_id <= 0 or not re.fullmatch(r"[a-f0-9-]{36}", checkout_id):
            raise RepositoryError("checkout_invalid", "Invalid managed checkout identity")
        return self.directory / str(user_id) / checkout_id

    def _private_directory(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RepositoryError("checkout_invalid", "Managed checkout storage must be private and owned by the backend operator")
        if directory.resolve(strict=True) != directory:
            raise RepositoryError("checkout_invalid", "Managed checkout storage cannot traverse symbolic links")

    async def baseline(self, source: Path) -> str:
        await self._validate_root(source)
        commit, _ = await self._git(source, "rev-parse", "--verify", "HEAD^{commit}")
        baseline = commit.decode("ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{40,64}", baseline):
            raise RepositoryError("checkout_invalid", "Repository needs a valid committed baseline")
        return baseline

    async def create(self, source: Path, target: Path, reason: str, *, baseline: str | None = None) -> str:
        async with asyncio.timeout(60), self._capacity:
            await self._validate_root(source)
            self._private_directory(self.directory)
            self._private_directory(target.parent)
            if target.exists() or target.is_symlink():
                raise RepositoryError("checkout_exists", "Checkout path already exists; it will not be reused or overwritten")
            baseline = baseline or await self.baseline(source)
            if not re.fullmatch(r"[a-f0-9]{40,64}", baseline):
                raise RepositoryError("checkout_invalid", "Repository needs a valid committed baseline")
            # Detach at the immutable commit. Dirty/ignored source files are not
            # copied. Filters/hooks/network/submodule recursion are disabled by
            # the shared Git runner, including during checkout population.
            await self._git(source, "worktree", "add", "--detach", "--lock", "--reason", reason, str(target), baseline)
            await self.validate(source, target, baseline)
            dirty, _ = await self._git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            if dirty:
                raise RepositoryError("checkout_invalid", "Created checkout is not clean; it has been retained for inspection")
            return baseline

    async def validate(self, source: Path, target: Path, baseline: str, *, head_commit: str | None = None,
                       branch_name: str | None = None) -> None:
        if target.resolve(strict=True) != target or not target.is_dir() or not (target / ".git").is_file() or (target / ".git").is_symlink():
            raise RepositoryError("checkout_invalid", "Managed checkout is missing or changed")
        self._private_directory(self.directory)
        self._private_directory(target.parent)
        await self._validate_root(target)
        common, _ = await self._git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        actual, _ = await self._git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common != actual:
            raise RepositoryError("checkout_invalid", "Checkout no longer belongs to its authorized repository")
        head, _ = await self._git(target, "rev-parse", "--verify", "HEAD")
        branch, _ = await self._git(target, "symbolic-ref", "-q", "HEAD", allowed=(0, 1))
        expected_branch = (f"refs/heads/{branch_name}\n".encode() if branch_name else b"")
        if head.decode("ascii").strip() != (head_commit or baseline) or branch != expected_branch:
            raise RepositoryError("checkout_invalid", "Managed checkout baseline or detached state changed")
        if branch_name and not re.fullmatch(r"fruitcake/[a-f0-9-]{36}", branch_name):
            raise RepositoryError("checkout_invalid", "Unexpected managed branch")
        await self._git(target, "merge-base", "--is-ancestor", baseline, head_commit or baseline)

    async def validate_ready(self, source: Path, target: Path, baseline: str, *, head_commit: str | None = None,
                             branch_name: str | None = None) -> None:
        async with asyncio.timeout(15), self._capacity:
            await self.validate(source, target, baseline, head_commit=head_commit, branch_name=branch_name)

    async def remove(self, source: Path, target: Path, baseline: str | None, reason: str, *,
                     head_commit: str | None = None, branch_name: str | None = None) -> None:
        async with asyncio.timeout(60), self._capacity:
            await self._validate_root(source)
            if not target.exists() and not target.is_symlink():
                registrations, _ = await self._git(source, "worktree", "list", "--porcelain", "-z")
                if (b"worktree " + str(target).encode() + b"\0") not in registrations:
                    return  # A failed pre-check never registered/created anything.
                raise RepositoryError("checkout_invalid", "Checkout registration remains; operator recovery is required")
            await self.validate(source, target, baseline, head_commit=head_commit, branch_name=branch_name)
            dirty, _ = await self._git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
            if dirty:
                raise RepositoryError("checkout_dirty", "Checkout contains changed, untracked, or ignored files. Nothing was removed.")
            await self._git(source, "worktree", "unlock", str(target))
            try:
                # Never force removal, prune registrations, reset, or clean.
                await self._git(source, "worktree", "remove", str(target))
            except BaseException:
                try:
                    await self._git(source, "worktree", "lock", "--reason", reason, str(target))
                except (RepositoryError, OSError):
                    pass
                raise
