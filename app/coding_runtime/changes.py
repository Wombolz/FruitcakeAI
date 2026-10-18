"""Bounded, host-owned text changes. No runtime-supplied patch is executed."""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import os
from pathlib import Path
import secrets
import stat

from app.coding_runtime.repository import RepositoryError


CHANGE_PROTOCOL = "fruitcake.file_change.v1"
FILE_BYTES = 256 * 1024
# Complete snapshots/diffs need their own budget, separate from tool previews.
PROPOSAL_BYTES = 4 * 1024 * 1024
APPROVAL_BYTES = 4 * 1024 * 1024


def digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def text(value, *, missing=False) -> str | None:
    if value is None and missing:
        return None
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > FILE_BYTES:
        raise RepositoryError("change_invalid", "Changes require complete UTF-8 text files of at most 256 KiB. Do not truncate or shorten files to fit.")
    return value


@contextmanager
def parent(root: Path, path: str):
    if not isinstance(path, str) or len(path) > 1024 or any(ord(c) < 32 for c in path):
        raise RepositoryError("change_invalid", "Invalid change path.")
    parts = path.split("/")
    if any(not part or part in {".", ".."} or part.casefold() == ".git" for part in parts):
        raise RepositoryError("change_invalid", "Use a relative file path outside Git metadata.")
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
        yield descriptors[-1], parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_at(directory: int, name: str) -> tuple[str | None, int]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None, 0o644
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > FILE_BYTES:
            raise RepositoryError("change_invalid", "Only regular files without links, at most 256 KiB, can be changed. Do not truncate files to fit.")
        data = bytearray()
        while len(data) <= FILE_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return text(data.decode("utf-8")), stat.S_IMODE(metadata.st_mode) & 0o777
    finally:
        os.close(descriptor)


def validate(root: Path, path: str, before: str | None, after: str) -> None:
    text(before, missing=True)
    text(after)
    if before == after:
        raise RepositoryError("change_invalid", "The proposed change is empty.")
    with parent(root, path) as (directory, name):
        actual, _ = read_at(directory, name)
        if actual != before:
            raise RepositoryError("change_stale", "File changed since this proposal. Request a new approval.")


def patch(path: str, before: str | None, after: str) -> str:
    if before is None and after == "":
        return f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
    # Include all lines: approval is a complete, bounded preview, never a prefix.
    lines = difflib.unified_diff((before or "").splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile="a/" + path if before is not None else "/dev/null",
                                 tofile="b/" + path, n=FILE_BYTES)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def apply(root: Path, path: str, before: str | None, after: str) -> None:
    validate(root, path, before, after)
    with parent(root, path) as (directory, name):
        actual, mode = read_at(directory, name)
        if actual != before:
            raise RepositoryError("change_stale", "File changed while awaiting approval.")
        temporary = ".fruitcake-change-" + secrets.token_hex(16)
        replaced = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as output:
                output.write(after.encode("utf-8"))
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
            # Re-open the path chain immediately before replacement. Directory
            # handles avoid following a swapped symlink; this is not an OS sandbox.
            with parent(root, path) as (current, _):
                left, right = os.fstat(current), os.fstat(directory)
                if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino) or read_at(current, name)[0] != before:
                    raise RepositoryError("change_stale", "Change target moved or changed during approval.")
                if before is None:
                    # No overwrite if a new destination appeared after validation.
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    replaced = True
                    os.unlink(temporary, dir_fd=directory)
                else:
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    replaced = True
            os.fsync(directory)
            if read_at(directory, name)[0] != after:
                raise RepositoryError("change_uncertain", "The written file could not be verified. Inspect the checkout.")
        except (OSError, UnicodeError, RepositoryError) as exc:
            if replaced:
                raise RepositoryError("change_uncertain", "Replacement occurred but verification failed. Inspect the checkout.") from exc
            raise
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if replaced:
                    raise RepositoryError("change_uncertain", "Replacement occurred but temporary-file cleanup failed. Inspect the checkout.") from exc
                raise
