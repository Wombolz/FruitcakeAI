from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.models import Document, DocumentIngestJob
from app.rag.document_processor import DocumentProcessor
from app.rag.extractor import DocumentExtractor, ExtractionError
from app.rag.job_runner import (
    _claim_next_due_job,
    _run_claimed_job,
    enqueue_document_ingest,
    recover_stale_document_ingest_jobs,
)
from tests.conftest import TestSessionLocal


class FakeIndexSink:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.is_ready = True
        self.fail_times = fail_times
        self.calls: list[dict] = []
        self.deleted: list[int] = []

    async def ingest_text(
        self,
        *,
        text: str,
        document_id: int,
        user_id: int,
        scope: str,
        filename: str,
    ) -> int:
        self.calls.append(
            {
                "text": text,
                "document_id": document_id,
                "user_id": user_id,
                "scope": scope,
                "filename": filename,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("temporary ingest failure")
        return 1

    async def delete_document(self, document_id: int) -> None:
        self.deleted.append(document_id)


class FailingExtractor:
    def extract(self, file_path: Path) -> tuple[str, str]:
        raise ExtractionError("boom")

    def content_type_from_extension(self, file_path: Path) -> str:
        return "txt"


async def _headers(client, username: str) -> dict[str, str]:
    password = "pass123"
    await client.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    login = await client.post("/auth/login", json={"username": username, "password": password})
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _user_id(client, headers: dict[str, str]) -> int:
    me = await client.get("/auth/me", headers=headers)
    return int(me.json()["id"])


async def _create_document(
    *,
    owner_id: int,
    file_path: Path,
    original_filename: str | None = None,
    scope: str = "personal",
    status: str = "pending",
) -> int:
    async with TestSessionLocal() as db:
        doc = Document(
            owner_id=owner_id,
            filename=file_path.name,
            original_filename=original_filename or file_path.name,
            file_path=str(file_path),
            file_size_bytes=file_path.stat().st_size,
            mime_type="text/plain",
            scope=scope,
            processing_status=status,
            title=original_filename or file_path.name,
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return int(doc.id)


@pytest.mark.asyncio
async def test_pdf_extraction_not_raw_stream(tmp_path):
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "manual.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 720, "Stained Glass Adversarial Obfuscation Studio Manual")
    c.drawString(72, 700, "Identity Mode B preserves human familiarity.")
    c.save()

    extractor = DocumentExtractor()
    method, text = extractor.extract(pdf_path)

    assert method in {"pymupdf", "pypdf2", "ocr"}
    assert "Stained Glass Adversarial Obfuscation Studio Manual" in text
    assert "%PDF" not in text
    assert "%%EOF" not in text


@pytest.mark.asyncio
async def test_markdown_frontmatter_stripped(tmp_path):
    md_file = tmp_path / "test.md"
    md_file.write_text("---\ntitle: Test\ndate: 2026-01-01\n---\n# Hello\n\nThis is content.")

    extractor = DocumentExtractor()
    method, text = extractor.extract(md_file)

    assert method == "markdown"
    assert "Hello" in text
    assert "This is content" in text
    assert "title: Test" not in text
    assert "date: 2026-01-01" not in text


@pytest.mark.asyncio
async def test_single_source_extraction(tmp_path, client):
    headers = await _headers(client, "docsingle")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("The quick brown fox jumps over the lazy dog.")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file)

    sink = FakeIndexSink()
    processor = DocumentProcessor(index_sink=sink)

    async with TestSessionLocal() as db:
        await processor.process(
            db=db,
            document_id=doc_id,
            file_path=txt_file,
            user_id=user_id,
            scope="personal",
            filename=txt_file.name,
        )
        doc = await db.get(Document, doc_id)

    assert doc is not None
    assert sink.calls
    assert doc.content == sink.calls[0]["text"]


@pytest.mark.asyncio
async def test_ingest_lifecycle_transitions(tmp_path, client):
    headers = await _headers(client, "doclifecycle")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("First sentence. Second sentence. Third sentence.")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file)

    processor = DocumentProcessor(index_sink=FakeIndexSink())
    async with TestSessionLocal() as db:
        await processor.process(
            db=db,
            document_id=doc_id,
            file_path=txt_file,
            user_id=user_id,
            scope="personal",
            filename=txt_file.name,
        )
        doc = await db.get(Document, doc_id)

    assert doc is not None
    assert doc.processing_status == "ready"
    assert doc.processing_started_at is not None
    assert doc.processing_completed_at is not None
    assert doc.content is not None
    assert doc.summary is not None
    assert doc.chunk_count == 1


@pytest.mark.asyncio
async def test_upload_creates_queued_ingest_job(client, tmp_path):
    headers = await _headers(client, "docqueue")
    doc_file = tmp_path / "queued.txt"
    doc_file.write_text("queue me")

    with doc_file.open("rb") as handle:
        resp = await client.post(
            "/library/ingest",
            headers=headers,
            files={"file": ("queued.txt", handle, "text/plain")},
            data={"scope": "personal"},
        )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["status"] == "processing"
    assert payload["ingest_job_status"] == "queued"

    async with TestSessionLocal() as db:
        doc = await db.get(Document, int(payload["id"]))
        job = (
            await db.execute(
                select(DocumentIngestJob).where(DocumentIngestJob.document_id == int(payload["id"]))
            )
        ).scalar_one_or_none()

    assert doc is not None
    assert doc.processing_status == "processing"
    assert job is not None
    assert job.status == "queued"
    assert job.attempt_count == 0


@pytest.mark.asyncio
async def test_retryable_failure_requeues_job(tmp_path, client):
    headers = await _headers(client, "docretry")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "retry.txt"
    txt_file.write_text("Retry me.")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file)

    async with TestSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        assert doc is not None
        await enqueue_document_ingest(db, document=doc)
        await db.commit()

    processor = DocumentProcessor(index_sink=FakeIndexSink(fail_times=1))
    with (
        patch("app.rag.job_runner.AsyncSessionLocal", new=TestSessionLocal),
        patch("app.rag.job_runner.get_document_processor", return_value=processor),
    ):
        claimed = await _claim_next_due_job()
        assert claimed is not None
        await _run_claimed_job(claimed)

    async with TestSessionLocal() as db:
        job = (
            await db.execute(select(DocumentIngestJob).where(DocumentIngestJob.document_id == doc_id))
        ).scalar_one()
        doc = await db.get(Document, doc_id)

    assert job.status == "queued"
    assert job.attempt_count == 1
    assert job.last_error == "temporary ingest failure"
    assert job.next_attempt_at is not None
    assert doc is not None
    assert doc.processing_status == "processing"
    assert doc.error_message is None


@pytest.mark.asyncio
async def test_terminal_failure_marks_job_failed(tmp_path, client):
    headers = await _headers(client, "docmaxfail")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "bad.txt"
    txt_file.write_text("bad")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file)

    async with TestSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        assert doc is not None
        job = await enqueue_document_ingest(db, document=doc, max_attempts=1)
        await db.commit()
        assert job.max_attempts == 1

    processor = DocumentProcessor(extractor=FailingExtractor(), index_sink=FakeIndexSink())
    with (
        patch("app.rag.job_runner.AsyncSessionLocal", new=TestSessionLocal),
        patch("app.rag.job_runner.get_document_processor", return_value=processor),
    ):
        claimed = await _claim_next_due_job()
        assert claimed is not None
        await _run_claimed_job(claimed)

    async with TestSessionLocal() as db:
        job = (
            await db.execute(select(DocumentIngestJob).where(DocumentIngestJob.document_id == doc_id))
        ).scalar_one()
        doc = await db.get(Document, doc_id)

    assert job.status == "failed"
    assert job.attempt_count == 1
    assert job.last_error == "boom"
    assert doc is not None
    assert doc.processing_status == "error"
    assert doc.error_message == "boom"


@pytest.mark.asyncio
async def test_stale_running_job_recovery_requeues(client, tmp_path):
    headers = await _headers(client, "docrecover")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "recover.txt"
    txt_file.write_text("recover me")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file, status="processing")

    async with TestSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        assert doc is not None
        job = DocumentIngestJob(
            document_id=doc_id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            queued_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            claimed_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(job)
        await db.commit()

    with patch("app.rag.job_runner.AsyncSessionLocal", new=TestSessionLocal):
        recovered = await recover_stale_document_ingest_jobs(stale_threshold_minutes=15)

    assert recovered == 1
    async with TestSessionLocal() as db:
        job = (
            await db.execute(select(DocumentIngestJob).where(DocumentIngestJob.document_id == doc_id))
        ).scalar_one()
        doc = await db.get(Document, doc_id)

    assert job.status == "queued"
    assert "Recovered interrupted ingest" in (job.last_error or "")
    assert doc is not None
    assert doc.processing_status == "processing"


@pytest.mark.asyncio
async def test_reprocess_endpoint_requeues_existing_job(client, tmp_path):
    headers = await _headers(client, "docreprocess")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "redo.txt"
    txt_file.write_text("First sentence. Second sentence.")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file, status="error")

    class FakeRag:
        is_ready = True

        def __init__(self) -> None:
            self.deleted: list[int] = []

        async def delete_document(self, document_id: int) -> None:
            self.deleted.append(document_id)

    fake_rag = FakeRag()
    with patch("app.api.library.get_rag_service", return_value=fake_rag):
        resp = await client.post(f"/library/documents/{doc_id}/reprocess", headers=headers)

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["ingest_job_status"] == "queued"
    assert fake_rag.deleted == [doc_id]

    async with TestSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        job = (
            await db.execute(select(DocumentIngestJob).where(DocumentIngestJob.document_id == doc_id))
        ).scalar_one_or_none()

    assert doc is not None
    assert doc.processing_status == "processing"
    assert job is not None
    assert job.status == "queued"


@pytest.mark.asyncio
async def test_duplicate_enqueue_does_not_create_competing_jobs(client, tmp_path):
    headers = await _headers(client, "docdedupe")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "same.txt"
    txt_file.write_text("same job")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file)

    async with TestSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        assert doc is not None
        job1 = await enqueue_document_ingest(db, document=doc)
        await db.flush()
        job2 = await enqueue_document_ingest(db, document=doc)
        await db.commit()

    assert job1.id == job2.id
    async with TestSessionLocal() as db:
        jobs = (
            await db.execute(select(DocumentIngestJob).where(DocumentIngestJob.document_id == doc_id))
        ).scalars().all()
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_document_excerpts_require_ready_status(client, tmp_path):
    headers = await _headers(client, "docexcerpts")
    user_id = await _user_id(client, headers)
    txt_file = tmp_path / "pending.txt"
    txt_file.write_text("not ready")
    doc_id = await _create_document(owner_id=user_id, file_path=txt_file, status="processing")

    resp = await client.get(
        f"/library/documents/{doc_id}/excerpts",
        params={"q": "anything"},
        headers=headers,
    )

    assert resp.status_code == 409
    assert "not ready" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_scoped_access_unchanged(client, tmp_path):
    headers_a = await _headers(client, "docscopea")
    user_a = await _user_id(client, headers_a)
    headers_b = await _headers(client, "docscopeb")

    base = tmp_path
    personal = base / "personal.txt"
    family = base / "family.txt"
    shared = base / "shared.txt"
    for path in (personal, family, shared):
        path.write_text(path.stem)

    await _create_document(owner_id=user_a, file_path=personal, scope="personal")
    await _create_document(owner_id=user_a, file_path=family, scope="family")
    await _create_document(owner_id=user_a, file_path=shared, scope="shared")

    resp = await client.get("/library/documents", headers=headers_b)
    assert resp.status_code == 200
    names = {row["filename"] for row in resp.json()}
    assert "personal.txt" not in names
    assert "family.txt" in names
    assert "shared.txt" in names
