from __future__ import annotations

import pytest

from app.coding_runtime.repository import RepositoryError
from test_api import api_harness, headers
from test_repository import git, repository
from test_checkouts import settled


@pytest.mark.asyncio
async def test_create_repository_authorizes_committed_empty_root_and_checkout(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, _, factory, *_):
        root = tmp_path / "New Repository"
        response = await client.post("/coding/repositories", headers=headers(1), json={"user_id": 2, "path": str(root)})
        assert response.status_code == 201, response.text
        grant = response.json()
        assert grant["canonical_path"] == str(root)
        assert git(root, "symbolic-ref", "--short", "HEAD").strip() == b"main"
        assert git(root, "log", "-1", "--format=%s").strip() == b"Initial commit"
        assert git(root, "ls-tree", "-r", "HEAD") == b""
        assert git(root, "status", "--porcelain") == b""
        session = await service.create_session(user_id=2, workspace_id=grant["id"], model_profile_id=1)
        assert not session.edit_mode and not session.command_mode
        await service.create_checkout(user_id=2, session_id=session.id)
        assert (await settled(service, session.id)).status == "ready"
        assert not factory.instances


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "nonempty", "repository", "symlink"])
async def test_create_never_initializes_or_overwrites_existing_paths(tmp_path, kind):
    async with api_harness(tmp_path) as (client, *_):
        root = tmp_path / "existing"
        if kind == "repository":
            repository(root)
        elif kind == "symlink":
            root.symlink_to(tmp_path / "absent", target_is_directory=True)
        else:
            root.mkdir()
            if kind == "nonempty":
                (root / "keep.txt").write_text("KEEP")
        before = git(root, "rev-parse", "HEAD") if kind == "repository" else None
        response = await client.post("/coding/repositories", headers=headers(1), json={"user_id": 2, "path": str(root)})
        assert response.status_code == 409
        if kind == "repository":
            assert git(root, "rev-parse", "HEAD") == before
        elif kind == "symlink":
            assert root.is_symlink() and not root.exists()
        else:
            assert not (root / ".git").exists()
            if kind == "nonempty":
                assert (root / "keep.txt").read_text() == "KEEP"


@pytest.mark.asyncio
async def test_creation_validates_identity_paths_and_never_creates_missing_parents(tmp_path):
    async with api_harness(tmp_path) as (client, *_):
        root = tmp_path / "unauthorized"
        body = {"user_id": 2, "path": str(root)}
        assert (await client.post("/coding/repositories", json=body)).status_code == 403
        assert not root.exists()
        assert (await client.post("/coding/repositories", headers=headers(1), json={**body, "user_id": 999})).status_code == 403
        assert not root.exists()
        for path in ["relative", "/", str(tmp_path / "absent" / "child")]:
            assert (await client.post("/coding/repositories", headers=headers(1), json={**body, "path": path})).status_code == 422
        assert not (tmp_path / "absent").exists()


@pytest.mark.asyncio
async def test_repository_authorization_fails_early_for_wrong_roots_and_missing_commits(tmp_path):
    async with api_harness(tmp_path) as (client, *_):
        root = repository(tmp_path / "valid")
        child = root / "child"
        child.mkdir()
        empty = tmp_path / "empty"
        empty.mkdir()
        unborn = tmp_path / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q")
        for candidate in [child, empty, unborn]:
            response = await client.post("/coding/workspaces", headers=headers(1), json={
                "user_id": 2, "path": str(candidate), "require_repository": True,
            })
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "not_repository"
        response = await client.post("/coding/workspaces", headers=headers(1), json={
            "user_id": 2, "path": str(root), "require_repository": True,
        })
        assert response.status_code == 201


@pytest.mark.asyncio
async def test_setup_failure_keeps_new_folder_but_does_not_authorize_it(tmp_path, monkeypatch):
    async with api_harness(tmp_path) as (client, _, service, *_):
        async def failure(*args, **kwargs):
            raise RepositoryError("repository_unavailable", "Private Git failure")
        monkeypatch.setattr(service._repository, "_git", failure)
        root = tmp_path / "unfinished"
        response = await client.post("/coding/repositories", headers=headers(1), json={"user_id": 2, "path": str(root)})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "repository_creation_failed"
        assert "Private Git failure" not in response.text
        assert root.is_dir()
        assert str(root) not in {w["canonical_path"] for w in (await client.get("/coding/workspaces")).json()}
