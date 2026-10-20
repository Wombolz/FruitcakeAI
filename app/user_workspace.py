"""User-owned workspace inspection using shared bounded, no-follow filesystem IO."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat

from app.coding_runtime.file_browser import (CheckoutFileBrowser, FILE_BYTES, FILE_LINES,
    PAGE_ENTRIES, directory_at, fail, stamp, validate_path, verify_directory)
from app.config import settings

DOWNLOAD_BYTES = 20_000_000


def workspace_path_allowed(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 1024:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path) or any(char in path for char in ('"', "\\")):
        return False
    parts = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        return False
    if any(part.casefold() in {"credentials", "secrets"} for part in parts):
        return False
    name = parts[-1].casefold()
    return not (name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"))
                or any(word in name for word in ("credential", "secret", "id_rsa", "id_ed25519"))
                or name in {"env", "token.json", "tokens.json"})


class UserWorkspaceBrowser(CheckoutFileBrowser):
    path_policy = staticmethod(workspace_path_allowed)

    async def ignored(self, root, paths):
        # Workspace files are not a Git repository. Do not apply repository grants
        # or invoke Git merely because a user has a folder named build or storage.
        return set()

    @staticmethod
    def _describe(user_id):
        base = Path(settings.workspace_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        name = str(user_id)
        with directory_at(base, []) as parent:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                identity = [str(base), user_id, metadata.st_dev, metadata.st_ino]
                root_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            finally:
                os.close(descriptor)
        root = base / name
        reference = {"provider": "user_workspace", "root_id": root_id, "session_id": None,
                     "user_id": user_id, "label": "My Workspace", "read_only": True,
                     "byte_limit": FILE_BYTES, "line_limit": FILE_LINES, "entry_limit": PAGE_ENTRIES}
        return root, reference

    async def describe(self, user_id):
        async with asyncio.timeout(15):
            return await self._filesystem(self._describe, user_id)

    async def browse(self, user_id, root_id, **arguments):
        root, reference = await self.describe(user_id)
        if reference["root_id"] != root_id:
            fail("workspace_changed", "This workspace reference has changed. Open My Workspace again.")
        result = await self.inspect(root, **arguments)
        if await self.describe(user_id) != (root, reference):
            fail("workspace_changed", "The workspace changed during inspection. Refresh it.")
        return {**result, "basis": "user_workspace", "root": reference}

    @classmethod
    def _download(cls, root, path):
        parts = validate_path(path, policy=cls.path_policy)
        with directory_at(root, parts[:-1]) as parent:
            directory = os.fstat(parent)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    fail("file_denied", "Only regular workspace files can be downloaded.")
                if before.st_size > DOWNLOAD_BYTES:
                    fail("repository_limit", "Workspace previews/downloads are limited to 20 MB.")
                data = bytearray()
                while len(data) <= DOWNLOAD_BYTES:
                    chunk = os.read(descriptor, min(65536, DOWNLOAD_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > DOWNLOAD_BYTES:
                    fail("repository_limit", "Workspace previews/downloads are limited to 20 MB.")
                if stamp(before) != stamp(os.fstat(descriptor)) or stamp(before) != stamp(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)):
                    fail("file_changed", "The file changed while reading. Refresh it.")
                verify_directory(root, parts[:-1], directory)
                return bytes(data)
            finally:
                os.close(descriptor)

    async def download(self, user_id, root_id, path):
        root, reference = await self.describe(user_id)
        if reference["root_id"] != root_id:
            fail("workspace_changed", "This workspace reference has changed. Open My Workspace again.")
        async with asyncio.timeout(15), self._capacity:
            data = await self._filesystem(self._download, root, path)
        if await self.describe(user_id) != (root, reference):
            fail("workspace_changed", "The workspace changed while reading. Refresh it.")
        return data
