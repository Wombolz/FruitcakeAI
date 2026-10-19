"""Bounded, read-only Git inspection, independent of runtime/model assertions."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import stat


class RepositoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    index_status: str
    worktree_status: str
    original_path: str | None = None


class _OutputLimit(Exception):
    def __init__(self, prefix: bytes, *, previewable: bool):
        self.prefix = prefix
        self.previewable = previewable


class RepositoryInspector:
    """Trusted-local alpha tooling, not a sandbox for hostile Git repositories."""

    def __init__(self, *, git: str | None = None, timeout: float = 10,
                 status_limit: int = 256 * 1024, diff_limit: int = 128 * 1024,
                 file_limit: int = 1000):
        self.git = git or shutil.which("git", path=os.defpath)
        self.timeout = timeout
        self.status_limit = status_limit
        self.diff_limit = diff_limit
        self.file_limit = file_limit
        self._capacity = asyncio.Semaphore(2)

    async def inspect(self, root: Path, *, path: str | None = None) -> dict:
        # Queue time is bounded too. No session/runtime lock or DB transaction is
        # held while Git runs; inspection must not prevent Stop/approval delivery.
        try:
            async with asyncio.timeout(self.timeout), self._capacity:
                await self._validate_root(root)
                files = await self._files(root)
                if path is None:
                    head, _ = await self._git(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128))
                    return {
                        "basis": "current_worktree", "observed_at": _now(),
                        "head": head.decode("ascii").strip() or None,
                        "files": [asdict(file) for file in files],
                    }
                file = next((file for file in files if file.path == path), None)
                if file is None:
                    raise RepositoryError("repository_changed", "This file is no longer in the change list. Refresh Changes.")
                paths = [file.path] + ([file.original_path] if file.original_path else [])
                for candidate in paths:
                    self._validate_file(root, candidate)
                if "U" in (file.index_status, file.worktree_status) or (
                    file.index_status, file.worktree_status
                ) in {("A", "A"), ("D", "D")}:
                    raise RepositoryError("unsupported_file", "Resolve this merge conflict in an external editor before previewing it.")
                sections = []
                if file.index_status == "?":
                    sections.append(await self._diff(root, "untracked", "--no-index", "--", os.devnull, file.path))
                else:
                    if file.index_status != " ":
                        sections.append(await self._diff(root, "staged", "--cached", "--", *paths))
                    if file.worktree_status != " ":
                        sections.append(await self._diff(root, "unstaged", "--", *paths))
                for candidate in paths:
                    self._validate_file(root, candidate)
                return {
                    "basis": "current_worktree", "observed_at": _now(), "path": file.path,
                    "sections": sections,
                }
        except TimeoutError as exc:
            raise RepositoryError("repository_timeout", "Repository inspection timed out. Try a smaller repository or retry.") from exc
        except (OSError, UnicodeError) as exc:
            raise RepositoryError("repository_unavailable", "The repository could not be inspected safely.") from exc

    async def _validate_root(self, root: Path) -> None:
        if not self.git:
            raise RepositoryError("repository_unavailable", "Git is not installed on the backend.")
        top, _ = await self._git(root, "rev-parse", "--show-toplevel", allowed=(0, 128))
        # Git searches ancestors by default. Never silently inspect a broader
        # parent repository when the grant names only a child directory.
        if not top or Path(os.fsdecode(top.removesuffix(b"\n"))).resolve(strict=True) != root:
            raise RepositoryError("not_repository", "Changes requires an authorized Git repository root, not a subdirectory or bare repository.")

    async def _files(self, root: Path) -> list[ChangedFile]:
        data, _ = await self._git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
        records = data.split(b"\0")
        files = []
        index = 0
        while index < len(records) - 1:
            record = records[index].decode("utf-8", errors="strict")
            index += 1
            if len(record) < 4 or record[2] != " ":
                raise RepositoryError("repository_unavailable", "Git returned an invalid change list.")
            original = None
            if "R" in record[:2] or "C" in record[:2]:
                if index >= len(records) - 1:
                    raise RepositoryError("repository_unavailable", "Git returned an invalid rename record.")
                original = records[index].decode("utf-8", errors="strict")
                index += 1
            files.append(ChangedFile(record[3:], record[0], record[1], original))
            if len(files) > self.file_limit:
                raise RepositoryError("repository_limit", "The repository has too many changes to review here. Use an external editor.")
        return files

    @staticmethod
    def _validate_file(root: Path, supplied: str) -> None:
        path = Path(supplied)
        if path.is_absolute() or not path.parts or any(part in {"..", ".git"} for part in path.parts) or "\0" in supplied:
            raise RepositoryError("unsupported_file", "This path cannot be previewed safely.")
        current = root
        for part in path.parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue  # Deleted files can still have an index/HEAD diff.
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
            ):
                raise RepositoryError("unsupported_file", "Symbolic links, hard links, and special files are not previewed. Use an external editor.")
        if current.is_dir():
            raise RepositoryError("unsupported_file", "Submodule and directory diffs require an external editor.")

    async def _diff(self, root: Path, kind: str, *args: str) -> dict:
        data, truncated = await self._git(
            root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
            "--ignore-submodules=all", "--src-prefix=a/", "--dst-prefix=b/", *args,
            limit=self.diff_limit, preview=True, allowed=(0, 1),
        )
        # A patch can contain non-UTF8 source bytes; show a lossy preview, never
        # present this display text as a patch suitable for applying or exporting.
        text = data.decode("utf-8", errors="replace")
        return {"kind": kind, "text": text, "truncated": truncated}

    async def _git(self, root: Path, *args: str, limit: int | None = None,
                   preview: bool = False, allowed: tuple[int, ...] = (0,),
                   _config_only: bool = False, _index: str | None = None,
                   _literal_paths: bool = True) -> tuple[bytes, bool]:
        filters = []
        if not _config_only:
            names, _ = await self._git(root, "config", "--null", "--name-only", "--get-regexp",
                                       r"^filter\..*\.(clean|smudge|process|required)$",
                                       allowed=(0, 1), _config_only=True)
            keys = set(names.decode("utf-8").split("\0")) - {""}
            if len(keys) > 64:
                raise RepositoryError("repository_limit", "Too many repository filters to inspect safely.")
            for key in sorted(keys):
                filters.extend(("-c", key + ("=false" if key.endswith(".required") else "=")))
        environment = {
            "PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        if _literal_paths:
            environment["GIT_LITERAL_PATHSPECS"] = "1"
        if _index is not None:
            environment["GIT_INDEX_FILE"] = _index
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.git, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.untrackedCache=false",
            "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
            *filters, "-C", str(root), *args,
            env=environment, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Cancellation can arrive while asyncio is creating the transport.
            # Claim and reap the process instead of abandoning a late spawn.
            process = await spawning
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise

        async def read(stream, bound, *, previewable=False):
            result = bytearray()
            while chunk := await stream.read(8192):
                remaining = bound - len(result)
                result.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    raise _OutputLimit(bytes(result), previewable=previewable)
            return bytes(result)

        stdout = asyncio.create_task(read(process.stdout, limit or self.status_limit, previewable=True))
        stderr = asyncio.create_task(read(process.stderr, 16384))
        try:
            try:
                data, _ = await asyncio.gather(stdout, stderr)
            except _OutputLimit as exc:
                if preview and exc.previewable:
                    return exc.prefix, True
                raise RepositoryError("repository_limit", "Repository inspection exceeded its output limit. Use an external editor.") from exc
            code = await process.wait()
            if code not in allowed:
                raise RepositoryError("repository_unavailable", "Git could not inspect this repository. Refresh or open it in an external editor.")
            return data, False
        finally:
            # Kill the process group even if the main process has exited, so a
            # child cannot keep pipes open after cancellation/output/timeout.
            try:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS may report EPERM just before asyncio observes the
                    # exit. Only tolerate it if the child is actually reaped.
                    await asyncio.wait_for(process.wait(), 0.2)
            finally:
                for task in (stdout, stderr):
                    task.cancel()
                await asyncio.gather(stdout, stderr, return_exceptions=True)
                await asyncio.wait_for(process.communicate(), 2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
