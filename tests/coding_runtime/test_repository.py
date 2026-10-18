from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.coding_runtime.repository import RepositoryError, RepositoryInspector


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def repository(root):
    root.mkdir(exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    (root / "file.txt").write_text("original\n")
    git(root, "add", "file.txt")
    git(root, "commit", "-qm", "Baseline")
    return root


@pytest.mark.asyncio
async def test_clean_status_is_read_only(tmp_path):
    root = repository(tmp_path / "repo")
    index = root / ".git/index"
    before = index.read_bytes(), index.stat().st_mtime_ns
    result = await RepositoryInspector().inspect(root)
    assert result["basis"] == "current_worktree"
    assert result["head"] == git(root, "rev-parse", "HEAD").decode().strip()
    assert result["files"] == []
    assert before == (index.read_bytes(), index.stat().st_mtime_ns)


@pytest.mark.asyncio
async def test_staged_unstaged_and_untracked_are_separate(tmp_path):
    root = repository(tmp_path / "repo")
    (root / "file.txt").write_text("staged\n")
    git(root, "add", "file.txt")
    (root / "file.txt").write_text("unstaged\n")
    (root / "new.txt").write_text("untracked\n")
    inspector = RepositoryInspector()
    result = await inspector.inspect(root)
    assert [(f["path"], f["index_status"], f["worktree_status"]) for f in result["files"]] == [
        ("file.txt", "M", "M"), ("new.txt", "?", "?"),
    ]
    diff = await inspector.inspect(root, path="file.txt")
    assert [part["kind"] for part in diff["sections"]] == ["staged", "unstaged"]
    assert "+staged" in diff["sections"][0]["text"]
    assert "+unstaged" in diff["sections"][1]["text"]
    assert all(not part["truncated"] for part in diff["sections"])
    assert "+untracked" in (await inspector.inspect(root, path="new.txt"))["sections"][0]["text"]
    assert git(root, "diff", "--cached", "--name-only").decode().strip() == "file.txt"


@pytest.mark.asyncio
async def test_unborn_repository_and_staged_new_file(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "new.txt").write_text("new\n")
    git(tmp_path, "add", "new.txt")
    inspector = RepositoryInspector()
    assert (await inspector.inspect(tmp_path))["head"] is None
    assert "+new" in (await inspector.inspect(tmp_path, path="new.txt"))["sections"][0]["text"]


@pytest.mark.asyncio
async def test_rename_deleted_and_literal_paths(tmp_path):
    root = repository(tmp_path / "repo")
    git(root, "mv", "file.txt", "renamed.txt")
    inspector = RepositoryInspector()
    status = await inspector.inspect(root)
    assert status["files"][0]["original_path"] == "file.txt"
    patch = (await inspector.inspect(root, path="renamed.txt"))["sections"][0]["text"]
    assert "a/file.txt" in patch and "b/renamed.txt" in patch
    git(root, "reset", "--quiet")
    (root / "renamed.txt").unlink()
    assert "-original" in (await inspector.inspect(root, path="file.txt"))["sections"][0]["text"]
    for name in ["odd[1].txt", "--output=bad.txt", "line\nbreak.txt", "café.txt", "name&path.txt"]:
        (root / name).write_text("only this file\n")
        result = await inspector.inspect(root, path=name)
        assert result["path"] == name
        assert "+only this file" in result["sections"][0]["text"]


@pytest.mark.asyncio
async def test_does_not_inspect_parent_repository_or_non_repository(tmp_path):
    root = repository(tmp_path / "repo")
    subdir = root / "child"
    subdir.mkdir()
    for target in (tmp_path, subdir):
        with pytest.raises(RepositoryError) as error:
            await RepositoryInspector().inspect(target)
        assert error.value.code == "not_repository"


@pytest.mark.asyncio
async def test_diff_rejects_arbitrary_paths_and_disappeared_changes(tmp_path):
    root = repository(tmp_path / "repo")
    for path in ["../secret", str(tmp_path), ":(glob)*", "file.txt", ".git/config"]:
        with pytest.raises(RepositoryError) as error:
            await RepositoryInspector().inspect(root, path=path)
        assert error.value.code == "repository_changed"


@pytest.mark.asyncio
async def test_links_and_special_files_are_not_opened(tmp_path):
    root = repository(tmp_path / "repo")
    outside = tmp_path / "secret"
    outside.write_text("OUTSIDE SECRET")
    (root / "link").symlink_to(outside)
    os.link(outside, root / "hardlink")
    os.mkfifo(root / "pipe")
    inspector = RepositoryInspector()
    for name in ("link", "hardlink"):
        with pytest.raises(RepositoryError) as error:
            await inspector.inspect(root, path=name)
        assert error.value.code == "unsupported_file"
        assert "OUTSIDE SECRET" not in str(error.value)
    # Git doesn't list FIFOs; it still must not be opened via a guessed path.
    with pytest.raises(RepositoryError):
        await inspector.inspect(root, path="pipe")


@pytest.mark.asyncio
async def test_symlink_parent_of_deleted_file_is_denied(tmp_path):
    root = repository(tmp_path / "repo")
    child = root / "child"
    child.mkdir()
    (child / "file").write_text("tracked")
    git(root, "add", "child/file")
    git(root, "commit", "-qm", "Child")
    (child / "file").unlink()
    child.rmdir()
    child.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RepositoryError) as error:
        await RepositoryInspector().inspect(root, path="child/file")
    assert error.value.code == "unsupported_file"


@pytest.mark.asyncio
async def test_external_diff_textconv_fsmonitor_and_inherited_git_environment_are_disabled(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    marker = tmp_path / "executed"
    command = f"touch '{marker}'"
    git(root, "config", "diff.external", command)
    git(root, "config", "core.fsmonitor", command)
    git(root, "config", "diff.custom.textconv", command)
    git(root, "config", "filter.custom.clean", command)
    git(root, "config", "filter.custom.process", command)
    git(root, "config", "filter.custom.required", "true")
    (root / ".gitattributes").write_text("*.txt diff=custom filter=custom\n")
    (root / "file.txt").write_text("changed\n")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "not inherited")
    result = await RepositoryInspector().inspect(root, path="file.txt")
    assert "+changed" in result["sections"][0]["text"]
    assert not marker.exists()
    assert not (tmp_path / "wrong-index").exists()


@pytest.mark.asyncio
async def test_binary_and_empty_untracked_files(tmp_path):
    root = repository(tmp_path / "repo")
    (root / "binary").write_bytes(b"\0\xff\xfe\x01")
    (root / "empty").touch()
    inspector = RepositoryInspector()
    assert "Binary files" in (await inspector.inspect(root, path="binary"))["sections"][0]["text"]
    assert "new file mode" in (await inspector.inspect(root, path="empty"))["sections"][0]["text"]


@pytest.mark.asyncio
async def test_limits_never_present_partial_status_as_clean(tmp_path):
    root = repository(tmp_path / "repo")
    for name in ("a", "b", "c"):
        (root / name).write_text("new\n" * 1000)
    for inspector in [RepositoryInspector(file_limit=2), RepositoryInspector(status_limit=4)]:
        with pytest.raises(RepositoryError) as error:
            await inspector.inspect(root)
        assert error.value.code == "repository_limit"
    patch = await RepositoryInspector(diff_limit=128).inspect(root, path="a")
    assert patch["sections"][0]["truncated"]
    assert len(patch["sections"][0]["text"].encode()) <= 128


@pytest.mark.asyncio
async def test_timeout_and_cancellation_reap_subprocess(tmp_path):
    executable = tmp_path / "fake-git"
    pid_file = tmp_path / "pid"
    executable.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec /bin/sleep 30\n')
    executable.chmod(0o700)
    with pytest.raises(RepositoryError) as error:
        await RepositoryInspector(git=str(executable), timeout=1).inspect(tmp_path)
    assert error.value.code == "repository_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    task = asyncio.create_task(RepositoryInspector(git=str(executable)).inspect(tmp_path))
    previous = pid_file.read_text()
    async with asyncio.timeout(3):
        while pid_file.read_text() == previous:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.asyncio
async def test_diagnostic_flood_is_bounded_and_never_rendered_as_diff(tmp_path):
    executable = tmp_path / "fake-git"
    executable.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        "sys.stderr.write('PRIVATE_DIAGNOSTIC' * 10000)\nsys.stderr.flush()\ntime.sleep(30)\n"
    )
    executable.chmod(0o700)
    inspector = RepositoryInspector(git=str(executable))
    with pytest.raises(RepositoryError) as error:
        await asyncio.wait_for(inspector._git(tmp_path, "diff", preview=True, _config_only=True), 3)
    assert error.value.code == "repository_limit"
    assert "PRIVATE_DIAGNOSTIC" not in str(error.value)
