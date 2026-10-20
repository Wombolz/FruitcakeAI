import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import update

from app.api import workspace
from app.auth.jwt import create_access_token
from app.config import settings
from app.db.models import User
from app.db.session import Base, get_db
from app.coding_runtime.repository import RepositoryError
from app.user_workspace import UserWorkspaceBrowser, DOWNLOAD_BYTES


@asynccontextmanager
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr(settings, "jwt_secret_key", "workspace-tests-only-key-32-bytes-minimum")
    monkeypatch.setattr(workspace, "_workspace_browser", UserWorkspaceBrowser())
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db, db.begin():
        db.add_all([User(id=index, username=f"user{index}", email=f"u{index}@example.test", hashed_password="unused",
                        role="parent", is_active=True) for index in (1, 2)])
    app = FastAPI()
    app.include_router(workspace.router, prefix="/workspace")
    async def database():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
            headers={"Authorization": f"Bearer {create_access_token(1, 'user1', 'parent')}"}) as client:
            yield client, sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_root_and_browsing_require_auth_and_do_not_grant_other_users(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, _):
        assert (await client.get("/workspace/explorer", headers={"Authorization": ""})).status_code == 403
        response = await client.get("/workspace/explorer")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        root = response.json()
        assert root["provider"] == "user_workspace" and root["user_id"] == 1 and root["session_id"] is None
        assert str(tmp_path) not in str(root)
        own = Path(settings.workspace_dir) / "1"
        (own / "report.md").write_text("# My report\n")
        other = Path(settings.workspace_dir) / "2"
        other.mkdir(); (other / "private.txt").write_text("other user's file")
        query = {"root_id": root["root_id"]}
        listing = await client.get("/workspace/explorer/directory", params=query)
        assert [e["name"] for e in listing.json()["entries"]] == ["report.md"]
        file = await client.get("/workspace/explorer/file", params={**query, "path": "report.md"})
        assert file.json()["text"] == "# My report\n" and file.json()["basis"] == "user_workspace"
        headers = {"Authorization": f"Bearer {create_access_token(2, 'user2', 'parent')}"}
        assert (await client.get("/workspace/explorer/file", params={**query, "path": "report.md"}, headers=headers)).status_code == 409
        for path in ("../2/private.txt", str(other / "private.txt"), "x/../../2/private.txt", "./report.md"):
            assert (await client.get("/workspace/explorer/file", params={**query, "path": path})).status_code == 403


@pytest.mark.asyncio
async def test_workspace_policy_is_not_git_policy_but_still_hides_secrets_links_and_special_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    browser = UserWorkspaceBrowser()
    root, reference = await browser.describe(1)
    for folder in ("build", "workspace", "storage"):
        (root / folder).mkdir(); (root / folder / "report.txt").write_text("visible")
    (root / "config.json").write_text('{"display":"user file"}')
    for hidden in (".env", "credentials.txt", "tokens.json", "key.pem"):
        (root / hidden).write_text("do not list")
    (root / "alias").symlink_to(root / "config.json")
    (root / "fifo").touch(); (root / "fifo").unlink()
    import os
    os.mkfifo(root / "fifo")
    os.link(root / "config.json", root / "hardlink")
    result = await browser.browse(1, reference["root_id"], path="")
    assert [e["name"] for e in result["entries"]] == ["build", "storage", "workspace"]
    assert (await browser.browse(1, reference["root_id"], path="build/report.txt", file=True))["text"] == "visible"
    for path in (".env", "alias", "fifo", "hardlink", "credentials.txt", "key.pem"):
        with pytest.raises((RepositoryError, OSError)):
            await browser.browse(1, reference["root_id"], path=path, file=True)


@pytest.mark.asyncio
async def test_download_is_bounded_attachment_not_same_origin_executable_html(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, _):
        root = (await client.get("/workspace/explorer")).json()
        directory = Path(settings.workspace_dir) / "1"
        (directory / "page.html").write_text("<script>no execution</script>")
        response = await client.get("/workspace/explorer/download", params={"root_id": root["root_id"], "path": "page.html"})
        assert response.content == b"<script>no execution</script>"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]
        with (directory / "large.bin").open("wb") as file:
            file.truncate(DOWNLOAD_BYTES + 1)
        assert (await client.get("/workspace/explorer/download", params={"root_id": root["root_id"], "path": "large.bin"})).status_code == 413
        source = await client.get("/workspace/explorer/file", params={"root_id": root["root_id"], "path": "large.bin"})
        assert source.json()["status"] == "oversized" and source.json()["text"] is None


@pytest.mark.asyncio
async def test_pagination_version_and_root_replacement_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    browser = UserWorkspaceBrowser()
    root, reference = await browser.describe(1)
    for index in range(105):
        (root / f"{index:03}.txt").write_text(str(index))
    first = await browser.browse(1, reference["root_id"], path="")
    assert len(first["entries"]) == 100 and first["next_offset"] == 100
    second = await browser.browse(1, reference["root_id"], path="", offset=100, version=first["version"])
    assert len(second["entries"]) == 5 and second["next_offset"] is None
    (root / "changed.txt").write_text("new")
    with pytest.raises(RepositoryError, match="page changed"):
        await browser.browse(1, reference["root_id"], path="", offset=100, version=first["version"])
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(RepositoryError, match="reference has changed"):
        await browser.browse(1, reference["root_id"], path="")


@pytest.mark.asyncio
async def test_symlink_user_root_never_adopts_other_users_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    other = tmp_path / "2"; other.mkdir()
    (tmp_path / "1").symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        await UserWorkspaceBrowser().describe(1)


@pytest.mark.asyncio
async def test_revoked_user_is_rechecked_after_filesystem_wait(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, sessions):
        browser = workspace._workspace_browser
        original = browser.describe
        async def revoked(user_id):
            result = await original(user_id)
            async with sessions() as db, db.begin():
                await db.execute(update(User).where(User.id == user_id).values(is_active=False))
            return result
        monkeypatch.setattr(browser, "describe", revoked)
        assert (await client.get("/workspace/explorer")).status_code == 401


@pytest.mark.asyncio
async def test_explicit_attachment_paths_read_exact_explorer_file(tmp_path, monkeypatch):
    from app.agent.context import UserContext
    from app.mcp.servers.filesystem import call_tool

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    browser = UserWorkspaceBrowser()
    root, reference = await browser.describe(7)
    (root / "report.txt").write_text("root report")
    (root / "workspace").mkdir()
    (root / "workspace" / "report.txt").write_text("nested report")
    (root / "workspace" / "7").mkdir()
    (root / "workspace" / "7" / "report.txt").write_text("nested numeric report")
    user = UserContext(user_id=7, username="tester", role="parent")
    for path in ("report.txt", "workspace/report.txt", "workspace/7/report.txt"):
        inspected = await browser.browse(7, reference["root_id"], path=path, file=True)
        # The native draft uses this explicit legacy-compatible tool prefix;
        # a real workspace/ folder must not be stripped from the relative path.
        attachment_path = f"workspace/7/{path}"
        assert await call_tool("read_file", {"path": attachment_path}, user) == inspected["text"]
