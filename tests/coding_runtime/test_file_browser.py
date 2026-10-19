from __future__ import annotations

import asyncio
import os

import pytest

from app.coding_runtime.file_browser import CheckoutFileBrowser, FILE_BYTES
from app.coding_runtime.repository import RepositoryError
from app.db.models import CodingCheckout, CodingWorkspace, User
from test_api import api_harness, headers
from test_checkouts import settled
from test_repository import git, repository


@pytest.mark.asyncio
async def test_unchanged_nested_and_untracked_files_are_read_only_and_ignored_paths_stay_hidden(tmp_path):
    root = repository(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src/a&b.py").write_text("print('hello')\n")
    (root / "ignored.txt").write_text("DO NOT DISPLAY")
    git(root, "add", "ignored.txt")  # Even force-tracked ignored content is excluded.
    (root / ".gitignore").write_text("ignored.txt\nprivate/\n")
    (root / "private").mkdir()
    (root / "private/x.txt").write_text("PRIVATE")
    (root / "credentials.txt").write_text("CREDENTIALS")
    (root / ".env").write_text("SECRET")
    before = (root / ".git/index").read_bytes(), (root / ".git/index").stat().st_mtime_ns
    browser = CheckoutFileBrowser()
    listing = await browser.inspect(root, path="")
    assert [entry["name"] for entry in listing["entries"]] == ["src", "file.txt"]
    nested = await browser.inspect(root, path="src")
    assert nested["entries"][0]["path"] == "src/a&b.py"
    value = await browser.inspect(root, path="file.txt", file=True)
    assert value["status"] == "text" and value["text"] == "original\n"
    assert value["line_count"] == 2 and len(value["version"]) == 64
    assert before == ((root / ".git/index").read_bytes(), (root / ".git/index").stat().st_mtime_ns)
    for path in ("ignored.txt", "private/x.txt", "private"):
        with pytest.raises(RepositoryError) as error:
            await browser.inspect(root, path=path, file=path != "private")
        assert error.value.code == "file_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "src/../file.txt", "./file.txt", "src//x", ".git/config", ".env", "secrets/key", "x.pem", "x\x00y", "x\\y"])
async def test_denied_paths_cannot_be_reopened_directly(tmp_path, path):
    root = repository(tmp_path / "repo")
    with pytest.raises(RepositoryError) as error:
        await CheckoutFileBrowser().inspect(root, path=path, file=True)
    assert error.value.code == "file_denied"


@pytest.mark.asyncio
async def test_links_and_special_files_are_never_followed_or_listed(tmp_path):
    root = repository(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE")
    (root / "symlink").symlink_to(outside)
    (root / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / "hardlink")
    os.mkfifo(root / "fifo")
    browser = CheckoutFileBrowser()
    result = await browser.inspect(root, path="")
    assert [entry["name"] for entry in result["entries"]] == ["file.txt"]
    for path in ("symlink", "directory-link/outside.txt", "hardlink", "fifo"):
        with pytest.raises(RepositoryError) as error:
            await browser.inspect(root, path=path, file=True)
        assert error.value.code == "file_denied"


@pytest.mark.asyncio
async def test_binary_oversized_empty_and_missing_files_are_truthful(tmp_path):
    root = repository(tmp_path / "repo")
    browser = CheckoutFileBrowser()
    for name, data, status in [("empty.txt", b"", "text"), ("binary.bin", b"\0\xff", "binary"),
                               ("large.txt", b"x" * (FILE_BYTES + 1), "oversized"),
                               ("many-lines.txt", b"\n" * 12000, "oversized")]:
        (root / name).write_bytes(data)
        value = await browser.inspect(root, path=name, file=True)
        assert value["status"] == status and value["byte_count"] == len(data)
        assert value["text"] == ("" if status == "text" else None)
    with pytest.raises(RepositoryError) as error:
        await browser.inspect(root, path="missing.txt", file=True)
    assert error.value.code == "file_missing"


@pytest.mark.asyncio
async def test_directory_pagination_is_bounded_and_versioned(tmp_path):
    root = repository(tmp_path / "repo")
    for index in range(110):
        (root / f"item-{index:03}.txt").touch()
    browser = CheckoutFileBrowser()
    first = await browser.inspect(root, path="")
    assert len(first["entries"]) == 100 and first["next_offset"] == 100
    second = await browser.inspect(root, path="", offset=100, version=first["version"])
    assert len(second["entries"]) == 11 and second["next_offset"] is None
    with pytest.raises(RepositoryError, match="require the observed version"):
        await browser.inspect(root, path="", offset=100)
    (root / "another.txt").touch()
    with pytest.raises(RepositoryError) as error:
        await browser.inspect(root, path="", offset=100, version=first["version"])
    assert error.value.code == "file_changed"


@pytest.mark.asyncio
async def test_large_directory_fails_at_the_scan_bound_instead_of_silently_truncating(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    import app.coding_runtime.file_browser as module
    monkeypatch.setattr(module, "DIRECTORY_SCAN", 2)
    (root / "a.txt").touch()
    (root / "b.txt").touch()
    with pytest.raises(RepositoryError) as error:
        await CheckoutFileBrowser().inspect(root, path="")
    assert error.value.code == "repository_limit"


@pytest.mark.asyncio
async def test_cancelled_io_keeps_its_capacity_until_the_os_call_finishes():
    import threading
    browser = CheckoutFileBrowser()
    entered, release = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        release.wait(5)

    request = asyncio.create_task(browser._filesystem(blocking))
    await asyncio.to_thread(entered.wait, 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert browser._io_capacity._value == 1
    release.set()
    async with asyncio.timeout(2):
        while browser._io_capacity._value != 2:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_changed_directory_and_read_are_not_delivered(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    original = os.read

    def changed_read(descriptor, size):
        data = original(descriptor, size)
        (root / "file.txt").write_text("replaced\n")
        return data

    monkeypatch.setattr(os, "read", changed_read)
    with pytest.raises(RepositoryError) as error:
        # Exercise only the anchored file primitive; no subprocess calls under the patch.
        CheckoutFileBrowser._read(root, "file.txt")
    assert error.value.code == "file_changed"


@pytest.mark.asyncio
async def test_api_is_exact_checkout_bound_and_never_uses_source_fallback(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, _, factory, _, session, _):
        source = repository(tmp_path / "repo")
        endpoint = f"/coding/sessions/{session.id}/explorer"
        assert (await client.get(endpoint)).status_code == 409
        await service.create_checkout(user_id=2, session_id=session.id)
        checkout = await settled(service, session.id)
        root = await client.get(endpoint)
        assert root.status_code == 200 and root.json()["root_id"] == checkout.id
        assert "canonical_path" not in root.json()
        params = {"root_id": checkout.id, "path": "file.txt"}
        value = await client.get(endpoint + "/file", params=params)
        assert value.status_code == 200 and value.json()["text"] == "original\n"
        assert value.headers["cache-control"] == "no-store"
        assert (await client.get(endpoint + "/file", params={**params, "root_id": "wrong"})).status_code == 409
        assert (await client.get(endpoint + "/file", params={**params, "path": "../file.txt"})).status_code == 403
        for owner in (1, 3):
            assert (await client.get(endpoint + "/file", params=params, headers=headers(owner))).status_code == 404
        # Loss of a managed path fails closed, even when the source still has bytes.
        os.rename(checkout.canonical_path, checkout.canonical_path + "-moved")
        assert (source / "file.txt").exists()
        assert (await client.get(endpoint + "/file", params=params)).status_code == 409
        assert not factory.instances


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_user", [False, True])
async def test_access_revocation_during_read_prevents_delivery_and_keeps_db_wait_free(tmp_path, revoke_user):
    async with api_harness(tmp_path) as (client, _, service, sessions, factory, grant, session, authentication):
        repository(tmp_path / "repo")
        await service.create_checkout(user_id=2, session_id=session.id)
        checkout = await settled(service, session.id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._file_browser.inspect

        async def delayed(*args, **kwargs):
            value = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return value

        service._file_browser.inspect = delayed
        request = asyncio.create_task(client.get(f"/coding/sessions/{session.id}/explorer/file",
                                                  params={"root_id": checkout.id, "path": "file.txt"}))
        await asyncio.wait_for(entered.wait(), 5)
        assert not authentication["session"].in_transaction()
        assert not service._db_lock.locked()
        async with sessions() as db, db.begin():
            if revoke_user:
                (await db.get(User, 2)).is_active = False
            else:
                (await db.get(CodingWorkspace, grant.id)).is_active = False
        release.set()
        response = await request
        assert response.status_code == 403 and "original" not in response.text
        assert not factory.instances


@pytest.mark.asyncio
async def test_checkout_ownership_is_verified_before_root_validation(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, sessions, _, _, session, _):
        repository(tmp_path / "repo")
        await service.create_checkout(user_id=2, session_id=session.id)
        checkout = await settled(service, session.id)
        async with sessions() as db, db.begin():
            (await db.get(CodingCheckout, checkout.id)).user_id = 3

        async def must_not_touch_foreign_root(**kwargs):
            pytest.fail("A foreign checkout was touched before its owner was checked")

        service._execution_root = must_not_touch_foreign_root
        assert (await client.get(f"/coding/sessions/{session.id}/explorer")).status_code == 403


@pytest.mark.asyncio
async def test_root_metadata_rechecks_revocation_after_worktree_validation(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, sessions, _, grant, session, _):
        repository(tmp_path / "repo")
        await service.create_checkout(user_id=2, session_id=session.id)
        await settled(service, session.id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._execution_root

        async def delayed(**kwargs):
            root = await original(**kwargs)
            entered.set()
            await release.wait()
            return root

        service._execution_root = delayed
        request = asyncio.create_task(client.get(f"/coding/sessions/{session.id}/explorer"))
        await asyncio.wait_for(entered.wait(), 5)
        async with sessions() as db, db.begin():
            (await db.get(CodingWorkspace, grant.id)).is_active = False
        release.set()
        response = await request
        assert response.status_code == 403 and "root_id" not in response.text
