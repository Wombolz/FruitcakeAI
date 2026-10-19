"""Bounded retained Git/worktree content. Never reads live files on evidence access."""
from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from uuid import UUID, uuid4

from app.coding_runtime import changes
from app.coding_runtime.repository import RepositoryError, RepositoryInspector

PROTOCOL = "fruitcake.review_capture.v1"
SCOPES = {"checkout_changes", "uncommitted"}
SHA = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
HASH = re.compile(r"^[a-f0-9]{64}$")
DENIED_PARTS = {".git", ".pi", ".codex", ".claude", ".agents", ".ssh", ".aws", ".gnupg",
    "node_modules", "vendor", "__pycache__", ".venv", "venv", "storage", "workspace", ".pytest_cache",
    "build", "dist", ".build", "deriveddata", "credentials", "secrets", "model_cache"}


def fail(code, message):
    raise RepositoryError(code, message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def retained_patch(path, before, after, before_mode, after_mode):
    header = f"diff --git a/{path} b/{path}\n"
    if before is None:
        header += f"new file mode {after_mode}\n"
    elif after is None:
        header += f"deleted file mode {before_mode}\n"
    elif before_mode != after_mode:
        header += f"old mode {before_mode}\nnew mode {after_mode}\n"
    lines = difflib.unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
        fromfile="a/" + path if before is not None else "/dev/null",
        tofile="b/" + path if after is not None else "/dev/null", n=changes.FILE_BYTES)
    return header + "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def path_allowed(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 1024 or any(ord(c) < 32 for c in path) or any(c in path for c in ('"', "\\")):
        return False
    parts = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") or part.casefold() in DENIED_PARTS for part in parts):
        return False
    name = parts[-1].casefold()
    return not (name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"))
                or any(word in name for word in ("credential", "secret", "id_rsa", "id_ed25519"))
                or name in {"env", "config.json", "token.json", "tokens.json"})


@dataclass(frozen=True)
class CaptureLimits:
    changed_paths: int = 128
    context_files: int = 2048
    inventory_paths: int = 4096
    content_bytes: int = 32 * 1024 * 1024
    read_bytes: int = 64 * 1024 * 1024
    patch_bytes: int = 2 * 1024 * 1024
    manifest_bytes: int = 4 * 1024 * 1024
    seconds: float = 30


class CaptureBuilder:
    def __init__(self, limits=None):
        self.limits = limits or CaptureLimits()
        self.git = RepositoryInspector(status_limit=2 * 1024 * 1024)

    async def inventory(self, root, baseline, scope):
        if scope not in SCOPES:
            fail("invalid_request", "Unknown review scope")
        await self.git._validate_root(root)
        async def git(*args):
            value, _ = await self.git._git(root, *args)
            return value
        head = (await git("rev-parse", "--verify", "HEAD^{commit}")).decode().strip()
        base = baseline if scope == "checkout_changes" else head
        if not isinstance(base, str) or not SHA.fullmatch(base) or not SHA.fullmatch(head):
            fail("capture_unsupported", "A committed HEAD and recorded checkout baseline are required")
        base = (await git("rev-parse", "--verify", base + "^{commit}")).decode().strip()
        if await git("ls-files", "--unmerged", "-z"):
            fail("capture_unsupported", "Resolve merge conflicts before capturing a review")
        tree = {}
        for item in (await git("ls-tree", "-r", "-z", "--full-tree", base)).split(b"\0"):
            if item:
                meta, path = item.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
                tree[path.decode("utf-8")] = (mode, kind, oid)
        tracked = self.paths(await git("ls-files", "--cached", "-z"))
        untracked = self.paths(await git("ls-files", "--others", "--exclude-standard", "-z"))
        live = sorted(set(tracked) | set(untracked))
        ignored = set(self.paths(await git("ls-files", "--cached", "--ignored", "--exclude-standard", "-z")))
        changed = set(self.paths(await git("diff", "--name-only", "-z", "--no-color", "--no-renames", "--no-ext-diff", "--no-textconv", base, "--")))
        changed.update(untracked)
        if len(set(live) | set(tree) | changed) > self.limits.inventory_paths:
            fail("capture_limit", "Repository inventory exceeds the bounded capture limit")
        ignored.update(await self.historical_ignored(root, tree, set(live) | set(tree)))
        return {"head": head, "base": base, "tree": tree, "live": sorted(set(live)),
                "ignored": sorted(ignored), "changed": sorted(changed), "untracked": sorted(untracked)}

    async def historical_ignored(self, root, tree, paths):
        # Evaluate the baseline's ignore files with Git itself, not a lossy glob
        # translation. Only policy files are materialized; no source is checked out.
        policies = [path for path in tree if path.split("/")[-1] == ".gitignore"
                    and path_allowed(path.replace(".gitignore", "ignore-policy"))]
        if not policies:
            return set()
        if len(policies) > 128:
            fail("capture_limit", "Too many historical ignore policies")
        ignored, size = set(), 0
        with tempfile.TemporaryDirectory(prefix="fruitcake-review-policy-") as directory:
            policy_root = Path(directory).resolve()
            for path in policies:
                mode, kind, oid = tree[path]
                if kind != "blob" or mode not in {"100644", "100755"} or not path_allowed(path.replace(".gitignore", "ignore-policy")):
                    fail("capture_unsupported", "Historical ignore policy cannot be evaluated safely")
                raw, _ = await self.git._git(root, "cat-file", "blob", oid, limit=changes.FILE_BYTES)
                changes.text(raw.decode("utf-8"))
                size += len(raw)
                if size > 2 * 1024 * 1024:
                    fail("capture_limit", "Historical ignore policy exceeds its bound")
                target = policy_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            eligible = sorted(path for path in paths if path_allowed(path))
            for offset in range(0, len(eligible), 32):
                arguments = [str(policy_root / path) for path in eligible[offset:offset + 32]]
                raw, _ = await self.git._git(root, "-c", "core.quotePath=false", "--work-tree=" + str(policy_root),
                    "check-ignore", "--no-index", "--", *arguments, allowed=(0, 1), _literal_paths=False)
                for path in raw.decode("utf-8").splitlines():
                    ignored.add(str(Path(path).relative_to(policy_root)))
        return ignored

    def paths(self, raw):
        paths = raw.decode("utf-8", errors="strict").split("\0")
        return [path for path in paths if path]

    def read_live(self, root, path):
        # Descriptor-relative no-follow traversal; read_at rejects hardlinks,
        # special files, oversized files, NULs and invalid UTF-8 before publication.
        try:
            with changes.parent(root, path) as (directory, name):
                def identity():
                    try:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink
                    except FileNotFoundError:
                        return None
                before = identity()
                result = changes.read_at(directory, name)
                if identity() != before:
                    fail("capture_changed", "A file changed during capture")
                with changes.parent(root, path) as (current, _):
                    old, new = os.fstat(directory), os.fstat(current)
                    if (old.st_dev, old.st_ino) != (new.st_dev, new.st_ino):
                        fail("capture_changed", "A directory moved during capture")
                return result
        except FileNotFoundError:
            return None, 0o644

    async def scan(self, root, baseline, scope, selected):
        inventory = await self.inventory(root, baseline, scope)
        if len(selected) > self.limits.changed_paths or len(selected) != len(set(selected)):
            fail("capture_limit", "Select at most 128 distinct changed paths")
        if not set(selected) <= set(inventory["changed"]):
            fail("capture_changed", "Selected paths no longer match this scope; refresh options")
        blobs, files, context, exclusions, observations = {}, [], [], [], []
        omitted = {}
        content_size, patch_size, read_size = 0, 0, 0

        def omit(path, reason):
            omitted[reason] = omitted.get(reason, 0) + 1
            if len(exclusions) < 256:
                exclusions.append({"path": path[:1024], "reason": reason})

        def keep(data, *, patch=False):
            nonlocal content_size, patch_size
            key = digest(data)
            if key not in blobs:
                if patch:
                    patch_size += len(data)
                else:
                    content_size += len(data)
                if content_size > self.limits.content_bytes or patch_size > self.limits.patch_bytes:
                    fail("capture_limit", "Complete retained content/diff exceeds the capture budget")
                blobs[key] = data
            return {"sha256": key, "bytes": len(data)}

        for path in sorted(selected) + sorted(set(inventory["live"]) - set(selected)):
            chosen = path in selected
            tree_entry = inventory["tree"].get(path)
            if not path_allowed(path) or path in inventory["ignored"]:
                if chosen:
                    fail("capture_unsupported", "A selected path is excluded by the review privacy policy")
                omit(path, "denied_path")
                continue
            if not chosen and path in inventory["untracked"]:
                omit(path, "unselected_untracked")
                continue
            if not chosen and (len(context) >= self.limits.context_files or read_size >= self.limits.read_bytes):
                omit(path, "context_limit")
                continue
            try:
                after, mode = await asyncio.to_thread(self.read_live, root, path)
                read_size += len(after.encode()) if after is not None else 0
                if tree_entry and (tree_entry[1] != "blob" or tree_entry[0] not in ("100644", "100755")):
                    fail("capture_unsupported", "Links/submodules cannot be captured")
                before = None
                if chosen and tree_entry:
                    raw, _ = await self.git._git(root, "cat-file", "blob", tree_entry[2], limit=changes.FILE_BYTES)
                    before = changes.text(raw.decode("utf-8"))
            except (OSError, ValueError, UnicodeError, RepositoryError) as exc:
                if getattr(exc, "code", None) == "capture_changed":
                    raise
                if chosen:
                    fail("capture_unsupported", "A selected file is linked, binary, oversized, unreadable or unsupported")
                omit(path, "unsupported_file")
                continue
            observations.append((path, changes.digest(after), mode))
            if chosen:
                before_mode = tree_entry[0] if tree_entry else None
                after_mode = ("100755" if mode & 0o111 else "100644") if after is not None else None
                patch = retained_patch(path, before, after, before_mode, after_mode)
                files.append({"path": path, "before": keep(before.encode()) if before is not None else None,
                    "after": keep(after.encode()) if after is not None else None,
                    "before_mode": before_mode, "after_mode": after_mode,
                    "patch": keep(patch.encode(), patch=True)})
            elif after is not None:
                if len(context) >= self.limits.context_files or content_size + len(after.encode()) > self.limits.content_bytes:
                    omit(path, "context_limit")
                else:
                    context.append({"path": path, **keep(after.encode())})
        fingerprint = digest(encode({"inventory": inventory, "observations": observations}))
        return {"base": inventory["base"], "head": inventory["head"], "files": files, "context": context,
            "exclusions": exclusions, "omission_counts": omitted, "fingerprint": fingerprint,
            "limited": bool(omitted)}, blobs

    async def options(self, root, baseline, scope):
        async with asyncio.timeout(self.limits.seconds):
            inventory = await self.inventory(root, baseline, scope)
        paths = inventory["changed"]
        return {"scope": scope, "base": inventory["base"], "head": inventory["head"],
            "eligibility_basis": "path_policy_only",
            "paths": [{"path": path[:1024], "eligible": path_allowed(path) and path not in inventory["ignored"]}
                      for path in paths[:self.limits.changed_paths]],
            "path_count": len(paths), "paths_limited": len(paths) > self.limits.changed_paths}

    async def capture(self, root, baseline, scope, selected, identity):
        async with asyncio.timeout(self.limits.seconds):
            manifest, blobs = await self.scan(root, baseline, scope, selected)
            check, _ = await self.scan(root, baseline, scope, selected)
            if manifest["fingerprint"] != check["fingerprint"]:
                fail("capture_changed", "Repository changed while capturing; refresh and try again")
            manifest.update(identity, protocol=PROTOCOL, scope=scope,
                captured_at=datetime.now(timezone.utc).isoformat())
            raw = encode(manifest)
            if len(raw) > self.limits.manifest_bytes:
                fail("capture_limit", "Capture manifest exceeds its size limit")
            return manifest, blobs


class CaptureStorage:
    def __init__(self, root):
        self.root = Path(root).absolute()

    @staticmethod
    def private(path, *, directory=False):
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail("capture_unavailable", "Capture storage is not private or has changed")

    def directory(self, owner, capture_id):
        if str(UUID(capture_id)) != capture_id or not isinstance(owner, int) or owner <= 0:
            fail("capture_unavailable", "Invalid capture identity")
        if self.root.resolve() != self.root:
            fail("capture_unavailable", "Capture storage moved behind a symbolic link")
        self.private(self.root, directory=True)
        parent = self.root / str(owner)
        self.private(parent, directory=True)
        return parent / capture_id

    def prepare(self, owner):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.private(self.root, directory=True)
        parent = self.root / str(owner)
        parent.mkdir(mode=0o700, exist_ok=True)
        self.private(parent, directory=True)

    @staticmethod
    def write(path, data):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def publish(self, owner, capture_id, manifest, blobs):
        self.prepare(owner)
        target = self.directory(owner, capture_id)
        temporary = target.parent / (".pending-" + str(uuid4()))
        temporary.mkdir(mode=0o700)
        try:
            for key, data in blobs.items():
                if not HASH.fullmatch(key) or digest(data) != key:
                    fail("capture_corrupt", "Invalid captured content")
                self.write(temporary / key, data)
            raw = encode(manifest)
            self.write(temporary / "manifest.json", raw)
            directory_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if target.exists() or target.is_symlink():
                fail("capture_corrupt", "Capture destination already exists")
            os.rename(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return digest(raw), len(raw) + sum(map(len, blobs.values()))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def read(self, directory, name, limit):
        self.private(directory, directory=True)
        with changes.parent(directory, name) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > limit:
                    fail("capture_corrupt", "Captured evidence has changed")
                result = bytearray()
                while chunk := os.read(fd, min(65536, limit + 1 - len(result))):
                    result.extend(chunk)
                    if len(result) > limit:
                        fail("capture_corrupt", "Captured evidence exceeds its bound")
                return bytes(result)
            finally:
                os.close(fd)

    def load(self, owner, capture_id, expected_hash):
        directory = self.directory(owner, capture_id)
        raw = self.read(directory, "manifest.json", CaptureLimits().manifest_bytes)
        if digest(raw) != expected_hash:
            fail("capture_corrupt", "Capture manifest integrity check failed")
        manifest = json.loads(raw)
        if manifest.get("capture_id") != capture_id or manifest.get("user_id") != owner or manifest.get("protocol") != PROTOCOL:
            fail("capture_corrupt", "Capture identity does not match retained evidence")
        return manifest

    def blob(self, owner, capture_id, entry):
        if not HASH.fullmatch(entry["sha256"]):
            fail("capture_corrupt", "Invalid retained content hash")
        raw = self.read(self.directory(owner, capture_id), entry["sha256"], max(changes.FILE_BYTES, CaptureLimits().patch_bytes))
        if digest(raw) != entry["sha256"] or len(raw) != entry["bytes"]:
            fail("capture_corrupt", "Retained content integrity check failed")
        return raw.decode("utf-8", errors="strict")

    def purge(self, owner, capture_id):
        try:
            directory = self.directory(owner, capture_id)
        except FileNotFoundError:
            return
        if directory.exists():
            self.private(directory, directory=True)
            shutil.rmtree(directory)
