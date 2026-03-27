from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Document, LinkedSource
from app.rag.extractor import DocumentExtractor
from tests.conftest import TestSessionLocal


async def _token(client, username: str) -> str:
    await client.post(
        "/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "pass123",
        },
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "pass123"},
    )
    return login.json()["access_token"]


@pytest.mark.asyncio
async def test_extractor_supports_python_and_yaml(tmp_path):
    py_file = tmp_path / "agent.py"
    py_file.write_text("def run():\n    return 'ok'\n")
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("name: fruitcake\nmode: test\n")

    extractor = DocumentExtractor()

    py_method, py_text = extractor.extract(py_file)
    yaml_method, yaml_text = extractor.extract(yaml_file)

    assert py_method == "plaintext"
    assert "def run" in py_text
    assert yaml_method == "plaintext"
    assert "name: fruitcake" in yaml_text
    assert extractor.content_type_from_extension(py_file) == "code"
    assert extractor.content_type_from_extension(yaml_file) == "config"


@pytest.mark.asyncio
async def test_link_file_creates_linked_source_and_document(client, tmp_path):
    token = await _token(client, "linkedfileuser")
    headers = {"Authorization": f"Bearer {token}"}

    code_file = tmp_path / "notes.py"
    code_file.write_text("print('hello')\n")

    resp = await client.post(
        "/library/link-file",
        headers=headers,
        json={"path": str(code_file), "scope": "personal"},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["source"]["source_type"] == "file"
    assert payload["source"]["document_count"] == 1
    assert payload["sync"]["created"] == 1
    assert payload["sync"]["queued"] == 1

    docs = await client.get("/library/documents", headers=headers)
    assert docs.status_code == 200
    listed = docs.json()
    assert listed[0]["source_mode"] == "linked"
    assert listed[0]["source_sync_status"] == "synced"
    assert listed[0]["source_path"] == str(code_file)


@pytest.mark.asyncio
async def test_link_folder_indexes_supported_files_and_lists_source(client, tmp_path):
    token = await _token(client, "linkedfolderuser")
    headers = {"Authorization": f"Bearer {token}"}

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    (repo / "config.toml").write_text("title = 'Fruitcake'\n")
    (repo / "README.md").write_text("# Repo\n")
    (repo / "logo.png").write_bytes(b"not-a-real-png")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["source"]["source_type"] == "folder"
    assert payload["source"]["document_count"] == 3
    assert payload["sync"]["created"] == 3

    sources = await client.get("/library/sources", headers=headers)
    assert sources.status_code == 200
    listed_sources = sources.json()
    assert len(listed_sources) == 1
    assert listed_sources[0]["root_path"] == str(repo)
    assert listed_sources[0]["document_count"] == 3

    docs = await client.get("/library/documents", headers=headers)
    names = sorted(d["filename"] for d in docs.json())
    assert names == ["README.md", "app.py", "config.toml"]


@pytest.mark.asyncio
async def test_rescan_marks_missing_linked_documents(client, tmp_path):
    token = await _token(client, "linkedrescanuser")
    headers = {"Authorization": f"Bearer {token}"}

    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "tracked.py"
    tracked.write_text("print('one')\n")

    link = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )
    assert link.status_code == 202
    source_id = link.json()["source"]["id"]

    tracked.unlink()

    rescan = await client.post(f"/library/sources/{source_id}/rescan", headers=headers)
    assert rescan.status_code == 202
    assert rescan.json()["sync"]["missing"] == 1

    async with TestSessionLocal() as db:
        source = (await db.execute(select(LinkedSource).where(LinkedSource.id == source_id))).scalar_one()
        doc = (await db.execute(select(Document).where(Document.linked_source_id == source_id))).scalar_one()

    assert source.sync_status == "ready"
    assert doc.source_sync_status == "missing"
