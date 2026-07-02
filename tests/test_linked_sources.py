from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
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
async def test_link_file_creates_linked_source_and_document(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedfileuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", "")

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
async def test_link_folder_respects_excluded_paths_and_reports_tree(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedfolderuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    storage = repo / "storage"
    app_dir = repo / "app"
    storage.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text("print('hi')\n")
    (storage / "old_run.md").write_text("old newspaper run\n")
    (repo / "README.md").write_text("# Repo\n")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal", "excluded_paths": ["storage"]},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["source"]["source_type"] == "folder"
    assert payload["source"]["document_count"] == 2
    assert payload["source"]["excluded_paths"] == ["storage"]

    detail = await client.get(f"/library/sources/{payload['source']['id']}", headers=headers)
    assert detail.status_code == 200
    tree = detail.json()["tree"]
    names = {node["name"] for node in tree}
    assert "app" in names
    assert "storage" in names
    storage_node = next(node for node in tree if node["name"] == "storage")
    assert storage_node["excluded"] is True

    docs = await client.get("/library/documents", headers=headers)
    filenames = sorted(d["filename"] for d in docs.json())
    assert filenames == ["README.md", "app/main.py"]


@pytest.mark.asyncio
async def test_link_folder_skips_env_files_by_default(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedsensitiveuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("OPENAI_API_KEY=secret\n")
    (repo / ".env.local").write_text("JWT_SECRET_KEY=secret\n")
    (repo / "app.py").write_text("print('hi')\n")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["source"]["document_count"] == 1

    docs = await client.get("/library/documents", headers=headers)
    assert docs.status_code == 200
    assert [d["filename"] for d in docs.json()] == ["app.py"]


@pytest.mark.asyncio
async def test_update_source_exclusions_removes_existing_subfolder_docs(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedremoveuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    (repo / "storage").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "storage" / "run1.md").write_text("stored result\n")
    (repo / "src" / "app.py").write_text("print('hi')\n")

    link = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )
    assert link.status_code == 202
    source_id = link.json()["source"]["id"]
    assert link.json()["source"]["document_count"] == 2

    update = await client.patch(
        f"/library/sources/{source_id}",
        headers=headers,
        json={"excluded_paths": ["storage"]},
    )
    assert update.status_code == 200
    assert update.json()["sync"]["removed"] == 1
    assert update.json()["source"]["excluded_paths"] == ["storage"]

    docs = await client.get("/library/documents", headers=headers)
    filenames = [d["filename"] for d in docs.json()]
    assert filenames == ["src/app.py"]

    async with TestSessionLocal() as db:
        source = (await db.execute(select(LinkedSource).where(LinkedSource.id == source_id))).scalar_one()
        remaining_docs = (await db.execute(select(Document).where(Document.linked_source_id == source_id))).scalars().all()

    assert source.excluded_paths == '["storage"]'
    assert len(remaining_docs) == 1


@pytest.mark.asyncio
async def test_empty_python_files_are_skipped_not_failed(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedemptyuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    pkg = repo / "package"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "worker.py").write_text("def run():\n    return True\n")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["sync"]["skipped_empty"] == 1
    assert payload["source"]["skipped_empty_count"] == 1
    assert payload["source"]["document_count"] == 1

    docs = await client.get("/library/documents", headers=headers)
    assert docs.status_code == 200
    assert [d["filename"] for d in docs.json()] == ["package/worker.py"]


@pytest.mark.asyncio
async def test_rescan_marks_missing_linked_documents(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedrescanuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

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


@pytest.mark.asyncio
async def test_link_folder_requires_allowed_roots_configuration(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedrootsrequired")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", "")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )

    assert resp.status_code == 400
    assert "LINKED_SOURCE_ALLOWED_ROOTS" in resp.text


@pytest.mark.asyncio
async def test_link_folder_rejects_path_outside_allowed_roots(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedrootsoutside")
    headers = {"Authorization": f"Bearer {token}"}

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(allowed_root))

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(outside_root), "scope": "personal"},
    )

    assert resp.status_code == 400
    assert "allowed import root" in resp.text


@pytest.mark.asyncio
async def test_link_folder_allows_multiple_configured_roots(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedrootsmulti")
    headers = {"Authorization": f"Bearer {token}"}

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    repo = second_root / "repo"
    repo.mkdir()
    (repo / "notes.py").write_text("print('hello')\n")
    monkeypatch.setattr(settings, "linked_source_allowed_roots", f"{first_root}, {second_root}")

    resp = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )

    assert resp.status_code == 202
    assert resp.json()["source"]["document_count"] == 1


@pytest.mark.asyncio
async def test_link_file_remains_allowed_without_folder_roots(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedfileroots")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", "")

    code_file = tmp_path / "notes.py"
    code_file.write_text("print('hello')\n")

    resp = await client.post(
        "/library/link-file",
        headers=headers,
        json={"path": str(code_file), "scope": "personal"},
    )

    assert resp.status_code == 202
    assert resp.json()["source"]["source_type"] == "file"


@pytest.mark.asyncio
async def test_link_file_rejects_sensitive_filenames(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedsensitiveuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", "")

    secret = tmp_path / ".env"
    secret.write_text("API_KEY=super-secret\n")

    resp = await client.post(
        "/library/link-file",
        headers=headers,
        json={"path": str(secret), "scope": "personal"},
    )

    assert resp.status_code == 400
    assert "sensitive" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_link_file_enforces_roots_when_configured(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedfilerootsuser")
    headers = {"Authorization": f"Bearer {token}"}

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inside = allowed_root / "inside.py"
    inside.write_text("print('in')\n")
    outside = tmp_path / "outside.py"
    outside.write_text("print('out')\n")

    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(allowed_root))

    rejected = await client.post(
        "/library/link-file",
        headers=headers,
        json={"path": str(outside), "scope": "personal"},
    )
    assert rejected.status_code == 400
    assert "allowed import root" in rejected.json()["error"]

    accepted = await client.post(
        "/library/link-file",
        headers=headers,
        json={"path": str(inside), "scope": "personal"},
    )
    assert accepted.status_code == 202


@pytest.mark.asyncio
async def test_rescan_skips_content_reads_for_unchanged_files(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedincruser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "steady.py").write_text("print('steady')\n")

    link = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )
    assert link.status_code == 202
    source_id = link.json()["source"]["id"]

    probed_paths: list[str] = []
    original_probe = DocumentExtractor.is_empty_textual_file

    def _spying_probe(self, file_path):
        probed_paths.append(str(file_path))
        return original_probe(self, file_path)

    monkeypatch.setattr(DocumentExtractor, "is_empty_textual_file", _spying_probe)

    rescan = await client.post(f"/library/sources/{source_id}/rescan", headers=headers)
    assert rescan.status_code == 202
    sync = rescan.json()["sync"]
    assert sync["unchanged"] == 1
    assert sync["updated"] == 0
    # The incremental path must decide from stored stat metadata alone —
    # unchanged files are never opened.
    assert probed_paths == []


@pytest.mark.asyncio
async def test_missing_then_reappearing_linked_file_is_reindexed(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedreappearuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

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

    hidden = tmp_path / "tracked.py.hidden"
    tracked.rename(hidden)

    gone = await client.post(f"/library/sources/{source_id}/rescan", headers=headers)
    assert gone.status_code == 202
    assert gone.json()["sync"]["missing"] == 1

    # Restore with identical content: rename preserves size and mtime, so
    # only the previously-missing status can trigger the re-index.
    hidden.rename(tracked)

    back = await client.post(f"/library/sources/{source_id}/rescan", headers=headers)
    assert back.status_code == 202
    assert back.json()["sync"]["updated"] == 1

    async with TestSessionLocal() as db:
        doc = (await db.execute(select(Document).where(Document.linked_source_id == source_id))).scalar_one()
    assert doc.source_sync_status == "synced"


@pytest.mark.asyncio
async def test_missing_backing_file_keeps_indexed_document_for_retrieval(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedcacheduser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()
    doomed = repo / "doomed.py"
    doomed.write_text("print('cache me')\n")

    link = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )
    assert link.status_code == 202
    source_id = link.json()["source"]["id"]

    deleted_from_index: list[int] = []

    class _RecordingRag:
        is_ready = True

        async def delete_document(self, document_id: int) -> None:
            deleted_from_index.append(document_id)

    import app.library_sources as library_sources

    monkeypatch.setattr(library_sources, "get_rag_service", lambda: _RecordingRag())

    doomed.unlink()
    rescan = await client.post(f"/library/sources/{source_id}/rescan", headers=headers)
    assert rescan.status_code == 202
    assert rescan.json()["sync"]["missing"] == 1

    # Cached retrieval contract: a missing backing path marks the document
    # stale but never evicts its indexed content.
    assert deleted_from_index == []

    docs = await client.get("/library/documents", headers=headers)
    assert docs.status_code == 200
    listed = [d for d in docs.json() if d["linked_source_id"] == source_id]
    assert len(listed) == 1
    assert listed[0]["source_sync_status"] == "missing"
    assert listed[0]["source_path"] == str(doomed)


@pytest.mark.asyncio
async def test_linked_document_exposes_source_path_for_citation(client, tmp_path, monkeypatch):
    token = await _token(client, "linkedcitationuser")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(settings, "linked_source_allowed_roots", str(tmp_path))

    repo = tmp_path / "repo"
    sub = repo / "app"
    sub.mkdir(parents=True)
    module = sub / "worker.py"
    module.write_text("def work():\n    return 1\n")

    link = await client.post(
        "/library/link-folder",
        headers=headers,
        json={"path": str(repo), "scope": "personal"},
    )
    assert link.status_code == 202

    docs = await client.get("/library/documents", headers=headers)
    assert docs.status_code == 200
    entries = {d["filename"]: d for d in docs.json()}
    entry = entries["app/worker.py"]
    assert entry["source_mode"] == "linked"
    assert entry["source_path"] == str(module)
