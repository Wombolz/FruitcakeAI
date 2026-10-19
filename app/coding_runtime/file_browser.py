"""Bounded current-checkout inspection. No model call, shell, writes, or path fallback."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from app.coding_runtime.path_policy import path_allowed
from app.coding_runtime.repository import RepositoryError, RepositoryInspector


FILE_BYTES = 256 * 1024
FILE_LINES = 12000
DIRECTORY_SCAN = 4096
PAGE_ENTRIES = 100


def fail(code, message):
    raise RepositoryError(code, message)


def validate_path(path, *, directory=False):
    if directory and path == "":
        return []
    if not path_allowed(path) or len(path.split("/")) > 32:
        fail("file_denied", "This relative path is excluded from source inspection.")
    return path.split("/")


def stamp(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


@contextmanager
def directory_at(root, parts):
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def verify_directory(root, parts, metadata):
    with directory_at(root, parts) as current:
        if stamp(os.fstat(current)) != stamp(metadata):
            fail("file_changed", "The directory changed during inspection. Refresh Explorer.")


class CheckoutFileBrowser:
    def __init__(self):
        self.git = RepositoryInspector(status_limit=256 * 1024)
        self._capacity = asyncio.Semaphore(2)
        self._io_capacity = asyncio.Semaphore(2)

    async def _filesystem(self, operation, *args):
        # Cancellation cannot interrupt an OS filesystem call. Keep its worker
        # slot until completion, even if the request has already timed out.
        await self._io_capacity.acquire()
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))

        def completed(task):
            self._io_capacity.release()
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(completed)
        return await asyncio.shield(worker)

    async def ignored(self, root, paths):
        ignored = set()
        for offset in range(0, len(paths), PAGE_ENTRIES):
            batch = paths[offset:offset + PAGE_ENTRIES]
            # check-ignore only accepts -z with stdin. Policy already excludes
            # control characters/quotes/backslashes, so unquoted line output is
            # unambiguous; ./ also prevents pathspec-looking names from changing meaning.
            data, _ = await self.git._git(root, "-c", "core.quotePath=false", "check-ignore", "--no-index",
                                        "--", *("./" + path for path in batch),
                                        allowed=(0, 1), _literal_paths=False)
            ignored.update(os.fsdecode(path).removeprefix("./") for path in data.splitlines() if path)
        return ignored

    async def inspect(self, root: Path, *, path: str, file=False, offset=0, version=None):
        validate_path(path, directory=not file)
        if type(offset) is not int or not 0 <= offset <= DIRECTORY_SCAN:
            fail("invalid_request", "Invalid directory offset.")
        try:
            async with asyncio.timeout(15), self._capacity:
                await self._filesystem(self._validate_target, root, path, file)
                if path and await self.ignored(root, [path]):
                    fail("file_denied", "This path is ignored by the repository policy.")
                if file:
                    result = await self._filesystem(self._read, root, path)
                else:
                    entries, signature = await self._filesystem(self._list, root, path)
                    # check-ignore --no-index also excludes force-tracked ignored files.
                    ignored = await self.ignored(root, [entry["path"] for entry in entries])
                    entries = [entry for entry in entries if entry["path"] not in ignored]
                    fingerprint = hashlib.sha256(json.dumps([signature, entries], sort_keys=True).encode()).hexdigest()
                    if version is not None and version != fingerprint:
                        fail("file_changed", "The directory page changed. Refresh before loading more.")
                    if offset and version is None:
                        fail("invalid_request", "Further directory pages require the observed version.")
                    page = entries[offset:offset + PAGE_ENTRIES]
                    result = {"path": path, "version": fingerprint, "entries": page,
                              "next_offset": offset + len(page) if offset + len(page) < len(entries) else None}
                if path and await self.ignored(root, [path]):
                    fail("file_denied", "Repository ignore policy changed during inspection.")
                return {"basis": "current_worktree", "observed_at": datetime.now(timezone.utc), **result}
        except TimeoutError as exc:
            raise RepositoryError("repository_timeout", "Source inspection timed out. Retry explicitly.") from exc
        except FileNotFoundError as exc:
            raise RepositoryError("file_missing", "The file or directory no longer exists. Refresh Explorer.") from exc
        except (OSError, UnicodeError) as exc:
            raise RepositoryError("file_denied", "The path could not be inspected without following links or special files.") from exc

    @staticmethod
    def _validate_target(root, path, file):
        parts = validate_path(path, directory=not file)
        with directory_at(root, parts[:-1] if file else parts) as directory:
            if file:
                metadata = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    fail("file_denied", "Only regular files without symbolic or hard links are displayed.")

    @staticmethod
    def _list(root, path):
        parts = validate_path(path, directory=True)
        entries = []
        with directory_at(root, parts) as directory:
            before = os.fstat(directory)
            with os.scandir(directory) as scan:
                for index, entry in enumerate(scan):
                    if index >= DIRECTORY_SCAN:
                        fail("repository_limit", "This directory exceeds the 4,096-entry inspection bound.")
                    relative = path + "/" + entry.name if path else entry.name
                    if not path_allowed(relative) or len(relative.split("/")) > 32:
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        kind = "file"
                    else:
                        continue
                    entries.append({"name": entry.name, "path": relative, "kind": kind,
                                    "size_bytes": metadata.st_size if kind == "file" else None})
            verify_directory(root, parts, before)
            signature = stamp(before)
        entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold(), entry["name"]))
        return entries, signature

    @staticmethod
    def _read(root, path):
        parts = validate_path(path)
        with directory_at(root, parts[:-1]) as directory:
            parent = os.fstat(directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    fail("file_denied", "Only regular files without symbolic or hard links are displayed.")
                result = {"path": path, "byte_count": before.st_size, "version": None,
                          "text": None, "line_count": None, "status": "oversized"}
                if before.st_size <= FILE_BYTES:
                    data = bytearray()
                    while len(data) <= FILE_BYTES:
                        chunk = os.read(descriptor, min(64 * 1024, FILE_BYTES + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if len(data) > FILE_BYTES:
                        fail("file_changed", "The file grew during inspection. Refresh it.")
                    try:
                        text = data.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        text = None
                    if text is None or "\0" in text:
                        result["status"] = "binary"
                    elif text.count("\n") + 1 <= FILE_LINES:
                        result.update(status="text", text=text, line_count=text.count("\n") + 1,
                                      version=hashlib.sha256(data).hexdigest())
                after = os.fstat(descriptor)
                current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if stamp(before) != stamp(after) or stamp(before) != stamp(current):
                    fail("file_changed", "The file changed during inspection. Refresh it.")
                verify_directory(root, parts[:-1], parent)
                return result
            finally:
                os.close(descriptor)
