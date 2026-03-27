from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Document, LinkedSource
from app.rag.document_processor import get_document_processor
from app.rag.extractor import DocumentExtractor, ExtractionError
from app.rag.job_runner import enqueue_document_ingest

_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_source_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def _stat_mtime_to_utc(stat_result: Any) -> datetime:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)


def _display_name_for(source: LinkedSource, file_path: Path) -> str:
    if source.source_type == "file":
        return file_path.name
    return str(file_path.relative_to(Path(source.root_path)))


def _should_skip_dir(path: Path) -> bool:
    return path.name in _SKIP_DIR_NAMES


def _iter_supported_files(root: Path, extractor: DocumentExtractor) -> list[Path]:
    supported: list[Path] = []
    for current_root, dirnames, filenames in __import__("os").walk(root):
        dir_path = Path(current_root)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        if _should_skip_dir(dir_path):
            continue
        for filename in filenames:
            file_path = dir_path / filename
            if extractor.supports(file_path):
                supported.append(file_path)
    supported.sort()
    return supported


async def create_linked_source(
    db: AsyncSession,
    *,
    user_id: int,
    path: str,
    source_type: str,
    scope: str,
) -> tuple[LinkedSource, dict[str, int]]:
    normalized = _normalize_source_path(path)
    extractor = get_document_processor()._extractor  # reuse configured extractor

    if source_type == "file":
        if not normalized.exists() or not normalized.is_file():
            raise ValueError("Linked file path does not exist or is not a file.")
        if not extractor.supports(normalized):
            raise ExtractionError(f"Unsupported file format: {normalized.name}")
    elif source_type == "folder":
        if not normalized.exists() or not normalized.is_dir():
            raise ValueError("Linked folder path does not exist or is not a directory.")
    else:
        raise ValueError("source_type must be 'file' or 'folder'.")

    source = LinkedSource(
        owner_id=user_id,
        name=normalized.name or str(normalized),
        source_type=source_type,
        root_path=str(normalized),
        scope=scope,
        sync_status="pending",
        error_message=None,
    )
    db.add(source)
    await db.flush()
    stats = await rescan_linked_source(db, source=source, user_id=user_id)
    return source, stats


async def list_linked_sources(db: AsyncSession, *, user_id: int) -> list[LinkedSource]:
    result = await db.execute(
        select(LinkedSource)
        .options(selectinload(LinkedSource.documents))
        .where(LinkedSource.owner_id == user_id)
        .order_by(LinkedSource.created_at.desc(), LinkedSource.id.desc())
    )
    return list(result.scalars().all())


async def get_linked_source(db: AsyncSession, *, source_id: int, user_id: int) -> LinkedSource | None:
    result = await db.execute(
        select(LinkedSource)
        .options(selectinload(LinkedSource.documents).selectinload(Document.ingest_job))
        .where(LinkedSource.id == source_id, LinkedSource.owner_id == user_id)
    )
    return result.scalar_one_or_none()


async def rescan_linked_source(
    db: AsyncSession,
    *,
    source: LinkedSource,
    user_id: int,
) -> dict[str, int]:
    if source.owner_id != user_id:
        raise ValueError("Linked source does not belong to this user.")

    root = Path(source.root_path)
    now = _utcnow()
    extractor = get_document_processor()._extractor
    stats = {
        "created": 0,
        "queued": 0,
        "updated": 0,
        "unchanged": 0,
        "missing": 0,
    }

    existing_result = await db.execute(
        select(Document)
        .options(selectinload(Document.ingest_job))
        .where(Document.linked_source_id == source.id)
    )
    existing_docs = {doc.file_path: doc for doc in existing_result.scalars().all()}

    if source.source_type == "file":
        if not root.exists() or not root.is_file():
            source.sync_status = "missing"
            source.error_message = "Linked file is unavailable."
            source.last_scanned_at = now
            for doc in existing_docs.values():
                doc.source_sync_status = "missing"
                stats["missing"] += 1
            return stats

        result = await _sync_file_document(
            db,
            source=source,
            file_path=root,
            existing_doc=existing_docs.get(str(root)),
            now=now,
        )
        stats[result] += 1
        if result in {"created", "updated"}:
            stats["queued"] += 1
        source.sync_status = "ready"
        source.error_message = None
        source.last_scanned_at = now
        return stats

    if not root.exists() or not root.is_dir():
        source.sync_status = "missing"
        source.error_message = "Linked folder is unavailable."
        source.last_scanned_at = now
        for doc in existing_docs.values():
            doc.source_sync_status = "missing"
            stats["missing"] += 1
        return stats

    seen_paths: set[str] = set()
    for file_path in _iter_supported_files(root, extractor):
        file_key = str(file_path)
        seen_paths.add(file_key)
        result = await _sync_file_document(
            db,
            source=source,
            file_path=file_path,
            existing_doc=existing_docs.get(file_key),
            now=now,
        )
        stats[result] += 1
        if result in {"created", "updated"}:
            stats["queued"] += 1

    for file_key, doc in existing_docs.items():
        if file_key in seen_paths:
            continue
        doc.source_sync_status = "missing"
        stats["missing"] += 1

    source.sync_status = "ready"
    source.error_message = None
    source.last_scanned_at = now
    return stats


async def _sync_file_document(
    db: AsyncSession,
    *,
    source: LinkedSource,
    file_path: Path,
    existing_doc: Document | None,
    now: datetime,
) -> str:
    stat_result = file_path.stat()
    file_size = int(stat_result.st_size)
    modified_at = _stat_mtime_to_utc(stat_result)
    display_name = _display_name_for(source, file_path)
    mime_type, _ = mimetypes.guess_type(str(file_path))

    if existing_doc is None:
        doc = Document(
            owner_id=source.owner_id,
            linked_source_id=source.id,
            filename=file_path.name,
            original_filename=display_name,
            file_path=str(file_path),
            file_size_bytes=file_size,
            mime_type=mime_type,
            scope=source.scope,
            processing_status="pending",
            title=display_name,
            source_mode="linked",
            source_sync_status="synced",
            source_modified_at=modified_at,
            source_last_seen_at=now,
        )
        db.add(doc)
        await db.flush()
        await enqueue_document_ingest(db, document=doc)
        return "created"

    existing_doc.scope = source.scope
    existing_doc.file_path = str(file_path)
    existing_doc.filename = file_path.name
    existing_doc.original_filename = display_name
    existing_doc.title = display_name
    existing_doc.mime_type = mime_type
    existing_doc.source_mode = "linked"
    existing_doc.source_sync_status = "synced"
    existing_doc.source_last_seen_at = now

    changed = (
        existing_doc.file_size_bytes != file_size
        or existing_doc.source_modified_at != modified_at
        or existing_doc.processing_status == "error"
    )
    if not changed:
        return "unchanged"

    existing_doc.file_size_bytes = file_size
    existing_doc.source_modified_at = modified_at
    existing_doc.chunk_count = None
    existing_doc.content = None
    existing_doc.summary = None
    existing_doc.extracted_text_length = None
    existing_doc.extraction_method = None
    existing_doc.content_type = None
    existing_doc.error_message = None
    rag = __import__("app.rag.service", fromlist=["get_rag_service"]).get_rag_service()
    if rag.is_ready:
        await rag.delete_document(existing_doc.id)
    await enqueue_document_ingest(db, document=existing_doc)
    return "updated"
