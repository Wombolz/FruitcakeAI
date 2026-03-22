from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.context import UserContext
from app.config import settings
from app.mcp.servers.filesystem import call_tool, get_tools


def test_filesystem_server_exposes_read_and_write_tools():
    tool_names = [tool["name"] for tool in get_tools()]
    assert "list_directory" in tool_names
    assert "read_file" in tool_names
    assert "write_file" in tool_names


@pytest.mark.asyncio
async def test_list_directory_shows_user_workspace_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    user_root = Path(tmp_path) / "7"
    (user_root / "notes").mkdir(parents=True)
    (user_root / "notes" / "today.md").write_text("hello workspace")
    (user_root / "todo.txt").write_text("buy milk")
    user_context = UserContext(user_id=7, username="tester", role="parent")

    result = await call_tool("list_directory", {"path": "."}, user_context)
    assert "Contents of ." in result
    assert "[dir] notes/" in result
    assert "[file] todo.txt" in result


@pytest.mark.asyncio
async def test_write_and_read_file_inside_user_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    user_context = UserContext(user_id=7, username="tester", role="parent")

    write_result = await call_tool(
        "write_file",
        {"path": "notes/today.md", "content": "hello workspace"},
        user_context,
    )
    assert "Wrote" in write_result

    expected_path = Path(tmp_path) / "7" / "notes" / "today.md"
    assert expected_path.exists()
    assert expected_path.read_text() == "hello workspace"

    read_result = await call_tool("read_file", {"path": "notes/today.md"}, user_context)
    assert read_result == "hello workspace"


@pytest.mark.asyncio
async def test_read_file_blocks_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    user_context = UserContext(user_id=3, username="tester", role="parent")

    with pytest.raises(ValueError, match="within the user's workspace"):
        await call_tool("read_file", {"path": "../outside.txt"}, user_context)

    with pytest.raises(ValueError, match="within the user's workspace"):
        await call_tool("list_directory", {"path": "../outside"}, user_context)


@pytest.mark.asyncio
async def test_write_file_blocks_cross_user_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    other_user_file = Path(tmp_path) / "9" / "private.txt"
    other_user_file.parent.mkdir(parents=True, exist_ok=True)

    user_context = UserContext(user_id=8, username="tester", role="parent")
    with pytest.raises(ValueError, match="within the user's workspace"):
        await call_tool(
            "write_file",
            {"path": str(other_user_file), "content": "nope"},
            user_context,
        )


@pytest.mark.asyncio
async def test_filesystem_tools_require_user_context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    with pytest.raises(ValueError, match="authenticated user context"):
        await call_tool("read_file", {"path": "notes/a.md"}, None)


@pytest.mark.asyncio
async def test_read_file_respects_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "filesystem_mcp_max_read_bytes", 5)
    user_context = UserContext(user_id=4, username="tester", role="parent")

    target = Path(tmp_path) / "4" / "big.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("123456789", encoding="utf-8")

    result = await call_tool("read_file", {"path": "big.txt"}, user_context)
    assert "too large to read safely" in result
