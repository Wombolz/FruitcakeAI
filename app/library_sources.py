from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Document, LinkedSource
from app.rag.document_processor import get_document_processor
from app.rag.extractor import DocumentExtractor, ExtractionError
from app.rag.job_runner import enqueue_document_ingest
from app.rag.service import get_rag_service

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

_SENSITIVE_FILE_PREFIXES = (
    ".env",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_source_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path.resolve()


def _allowed_linked_source_roots() -> list[Path]:
    configured = str(settings.linked_source_allowed_roots or "").strip()
    if not configured:
        return []

    roots: list[Path] = []
    for raw in configured.split(","):
        cleaned = raw.strip()
        if not cleaned:
            continue
        root = Path(cleaned).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _ensure_path_within_allowed_roots(path: Path) -> None:
    allowed_roots = _allowed_linked_source_roots()
    if not allowed_roots:
        raise ValueError(
            "Linked folders are disabled until LINKED_SOURCE_ALLOWED_ROOTS is configured."
        )

    for root in allowed_roots:
        try:
            path.relative_to(root)
            return
        except ValueError:
            continue

    roots_text = ", ".join(str(root) for root in allowed_roots)
    raise ValueError(
        "Linked folders must be inside an allowed import root. "
        f"Configured roots: {roots_text}"
    )


def _stat_mtime_to_utc(stat_result: Any) -> datetime:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)


def _display_name_for(source: LinkedSource, file_path: Path) -> str:
    if source.source_type == "file":
        return file_path.name
    return _relative_path_str(Path(source.root_path), file_path)


def _relative_path_str(root: Path, target: Path) -> str:
    return str(PurePosixPath(target.relative_to(root).as_posix()))


def _should_skip_dir(path: Path) -> bool:
    return path.name in _SKIP_DIR_NAMES


def _is_sensitive_file(path: Path) -> bool:
    name = path.name
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _SENSITIVE_FILE_PREFIXES)


def _load_excluded_paths(source: LinkedSource) -> list[str]:
    raw = source.excluded_paths or "[]"
    try:
        values = json.loads(raw)
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = str(PurePosixPath(value.strip())).strip("/")
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    normalized.sort()
    return normalized


def _store_excluded_paths(source: LinkedSource, paths: list[str]) -> None:
    source.excluded_paths = json.dumps(sorted(dict.fromkeys(paths)))


def _normalize_excluded_paths(root: Path, excluded_paths: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in excluded_paths or []:
        raw = str(raw or "").strip()
        if not raw:
            continue
        candidate = PurePosixPath(raw)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise ValueError(f"Invalid excluded path: {raw}")
        resolved = root / Path(*candidate.parts)
        try:
            relative = _relative_path_str(root, resolved)
        except Exception as exc:
            raise ValueError(f"Excluded path is outside source root: {raw}") from exc
        if relative and relative not in normalized:
            normalized.append(relative)
    normalized.sort()
    return normalized


def _is_excluded(relative_path: str, excluded_paths: list[str]) -> bool:
    for excluded in excluded_paths:
        if relative_path == excluded or relative_path.startswith(excluded + "/"):
            return True
    return False


def _iter_supported_files(root: Path, extractor: DocumentExtractor, excluded_paths: list[str]) -> list[Path]:
    supported: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dir_path = Path(current_root)
        relative_dir = "" if dir_path == root else _relative_path_str(root, dir_path)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIR_NAMES
            and not d.startswith(".")
            and not _is_excluded("/".join(filter(None, [relative_dir, d])), excluded_paths)
        ]
        if _should_skip_dir(dir_path):
            continue
        for filename in filenames:
            file_path = dir_path / filename
            if _is_sensitive_file(file_path):
                continue
            if not extractor.supports(file_path):
                continue
            relative_path = _relative_path_str(root, file_path)
            if _is_excluded(relative_path, excluded_paths):
                continue
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
    excluded_paths: list[str] | None = None,
) -> tuple[LinkedSource, dict[str, int]]:
    normalized = _normalize_source_path(path)
    extractor = get_document_processor()._extractor

    if source_type == "file":
        if not normalized.exists() or not normalized.is_file():
            raise ValueError("Linked file path does not exist or is not a file.")
        if not extractor.supports(normalized):
            raise ExtractionError(f"Unsupported file format: {normalized.name}")
        normalized_excluded_paths: list[str] = []
    elif source_type == "folder":
        if not normalized.exists() or not normalized.is_dir():
            raise ValueError("Linked folder path does not exist or is not a directory.")
        _ensure_path_within_allowed_roots(normalized)
        normalized_excluded_paths = _normalize_excluded_paths(normalized, excluded_paths)
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
        excluded_paths=json.dumps(normalized_excluded_paths),
        skipped_empty_count=0,
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


async def update_linked_source_exclusions(
    db: AsyncSession,
    *,
    source: LinkedSource,
    user_id: int,
    excluded_paths: list[str],
) -> dict[str, int]:
    if source.owner_id != user_id:
        raise ValueError("Linked source does not belong to this user.")
    if source.source_type != "folder":
        raise ValueError("Exclusions are only supported for linked folders.")

    root = Path(source.root_path)
    normalized = _normalize_excluded_paths(root, excluded_paths)
    previous = set(_load_excluded_paths(source))
    added = [path for path in normalized if path not in previous]
    _store_excluded_paths(source, normalized)

    removed_documents = 0
    if added:
        docs = (
            await db.execute(select(Document).where(Document.linked_source_id == source.id))
        ).scalars().all()
        rag = get_rag_service()
        for doc in docs:
            relative_path = _relative_path_str(root, Path(doc.file_path)) if Path(doc.file_path).is_absolute() else doc.original_filename
            if not _is_excluded(relative_path, added):
                continue
            if rag.is_ready:
                await rag.delete_document(doc.id)
            await db.delete(doc)
            removed_documents += 1

    sync = await rescan_linked_source(db, source=source, user_id=user_id)
    sync["removed"] = removed_documents
    return sync


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
    excluded_paths = _load_excluded_paths(source)
    stats: dict[str, int] = {
        "created": 0,
        "queued": 0,
        "updated": 0,
        "unchanged": 0,
        "missing": 0,
        "skipped_empty": 0,
        "removed": 0,
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
            source.skipped_empty_count = 0
            for doc in existing_docs.values():
                doc.source_sync_status = "missing"
                stats["missing"] += 1
            return stats

        if extractor.is_empty_textual_file(root):
            source.sync_status = "ready"
            source.error_message = None
            source.last_scanned_at = now
            source.skipped_empty_count = 1
            for doc in existing_docs.values():
                if get_rag_service().is_ready:
                    await get_rag_service().delete_document(doc.id)
                await db.delete(doc)
                stats["removed"] += 1
            stats["skipped_empty"] = 1
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
        source.skipped_empty_count = 0
        return stats

    if not root.exists() or not root.is_dir():
        source.sync_status = "missing"
        source.error_message = "Linked folder is unavailable."
        source.last_scanned_at = now
        source.skipped_empty_count = 0
        for doc in existing_docs.values():
            doc.source_sync_status = "missing"
            stats["missing"] += 1
        return stats

    seen_paths: set[str] = set()
    for file_path in _iter_supported_files(root, extractor, excluded_paths):
        file_key = str(file_path)
        seen_paths.add(file_key)
        if extractor.is_empty_textual_file(file_path):
            stats["skipped_empty"] += 1
            continue
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
    source.skipped_empty_count = stats["skipped_empty"]
    return stats


async def build_linked_source_tree(source: LinkedSource) -> list[dict[str, Any]]:
    root: dict[str, dict[str, Any]] = {}
    excluded = set(_load_excluded_paths(source))
    for path in excluded:
        _insert_tree_path(root, path, node_type="folder", excluded=True)

    for doc in source.documents or []:
        relative = doc.original_filename or doc.filename
        if not relative:
            continue
        _insert_tree_path(
            root,
            relative,
            node_type="file",
            excluded=(doc.source_sync_status == "missing"),
            document_id=doc.id,
            processing_status=doc.processing_status,
            source_sync_status=doc.source_sync_status,
        )

    return _serialize_tree_nodes(root)


def _insert_tree_path(
    tree: dict[str, dict[str, Any]],
    relative_path: str,
    *,
    node_type: str,
    excluded: bool,
    document_id: int | None = None,
    processing_status: str | None = None,
    source_sync_status: str | None = None,
) -> None:
    parts = [part for part in PurePosixPath(relative_path).parts if part not in {".", ""}]
    if not parts:
        return
    cursor = tree
    for idx, part in enumerate(parts):
        is_leaf = idx == len(parts) - 1
        node = cursor.setdefault(
            part,
            {
                "name": part,
                "path": str(PurePosixPath(*parts[: idx + 1])),
                "type": "folder" if not is_leaf else node_type,
                "excluded": False,
                "document_id": None,
                "processing_status": None,
                "source_sync_status": None,
                "children": {},
            },
        )
        if is_leaf:
            node["type"] = node_type
            node["excluded"] = bool(node["excluded"] or excluded)
            node["document_id"] = document_id
            node["processing_status"] = processing_status
            node["source_sync_status"] = source_sync_status
        else:
            cursor = node["children"]


def _serialize_tree_nodes(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for name in sorted(nodes):
        node = nodes[name]
        children = _serialize_tree_nodes(node["children"])
        serialized.append(
            {
                "name": node["name"],
                "path": node["path"],
                "type": node["type"],
                "excluded": bool(node["excluded"]),
                "document_id": node["document_id"],
                "processing_status": node["processing_status"],
                "source_sync_status": node["source_sync_status"],
                "children": children,
                "child_count": len(children),
            }
        )
    return serialized


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
        or existing_doc.source_sync_status != "synced"
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
    rag = get_rag_service()
    if rag.is_ready:
        await rag.delete_document(existing_doc.id)
    await enqueue_document_ingest(db, document=existing_doc)
    return "updated"
