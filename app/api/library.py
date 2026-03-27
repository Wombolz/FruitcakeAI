"""
FruitcakeAI v5 — Library API
POST /library/ingest, GET /library/query, GET /library/documents, DELETE /library/documents/{id}
"""

import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import Document, LinkedSource, User
from app.db.session import get_db
from app.library_sources import (
    build_linked_source_tree,
    create_linked_source,
    get_linked_source,
    list_linked_sources,
    rescan_linked_source,
    update_linked_source_exclusions,
)
from app.rag.extractor import ExtractionError
from app.rag.job_runner import enqueue_document_ingest
from app.rag.service import get_rag_service

router = APIRouter()

_ALLOWED_SCOPES = {"personal", "family", "shared"}


class LinkSourceRequest(BaseModel):
    path: str
    scope: str = "personal"
    excluded_paths: List[str] = []


class UpdateLinkedSourceRequest(BaseModel):
    excluded_paths: List[str]


def _serialize_document(d: Document) -> Dict[str, Any]:
    return {
        "id": d.id,
        "filename": d.original_filename or d.filename,
        "scope": d.scope,
        "created_at": d.created_at.isoformat() if d.created_at else "",
        "processing_status": d.processing_status,
        "content_type": d.content_type,
        "chunk_count": d.chunk_count,
        "summary": d.summary,
        "ingest_job_status": d.ingest_job.status if d.ingest_job else None,
        "ingest_attempt_count": d.ingest_job.attempt_count if d.ingest_job else 0,
        "ingest_last_error": d.ingest_job.last_error if d.ingest_job else None,
        "source_mode": d.source_mode,
        "source_sync_status": d.source_sync_status,
        "linked_source_id": d.linked_source_id,
        "source_path": d.file_path,
        "source_last_seen_at": d.source_last_seen_at.isoformat() if d.source_last_seen_at else None,
        "source_modified_at": d.source_modified_at.isoformat() if d.source_modified_at else None,
    }


def _serialize_linked_source(source: LinkedSource) -> Dict[str, Any]:
    docs = list(source.documents or [])
    return {
        "id": source.id,
        "name": source.name,
        "source_type": source.source_type,
        "root_path": source.root_path,
        "scope": source.scope,
        "sync_status": source.sync_status,
        "error_message": source.error_message,
        "last_scanned_at": source.last_scanned_at.isoformat() if source.last_scanned_at else None,
        "excluded_paths": body_paths(source.excluded_paths),
        "skipped_empty_count": int(source.skipped_empty_count or 0),
        "document_count": len(docs),
        "ready_document_count": sum(1 for d in docs if d.processing_status == "ready"),
        "missing_document_count": sum(1 for d in docs if d.source_sync_status == "missing"),
        "created_at": source.created_at.isoformat() if source.created_at else "",
    }


def body_paths(raw: str | None) -> List[str]:
    import json

    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except Exception:
        return []

# ── POST /library/ingest ──────────────────────────────────────────────────────

@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    file: UploadFile = File(...),
    scope: str = Form("personal"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Upload a document; embedding runs in the background."""
    if scope not in _ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: {', '.join(_ALLOWED_SCOPES)}",
        )

    # ── Save file to disk ─────────────────────────────────────────────────────
    user_storage = Path(settings.storage_dir) / str(current_user.id)
    user_storage.mkdir(parents=True, exist_ok=True)

    safe_name = f"{uuid.uuid4()}_{file.filename}"
    file_path = user_storage / safe_name

    try:
        with file_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        await file.close()

    file_size = file_path.stat().st_size

    # ── Create DB record + durable ingest job ────────────────────────────────
    doc = Document(
        owner_id=current_user.id,
        filename=safe_name,
        original_filename=file.filename,
        file_path=str(file_path),
        file_size_bytes=file_size,
        mime_type=file.content_type,
        scope=scope,
        processing_status="pending",
        title=file.filename,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    job = await enqueue_document_ingest(db, document=doc)

    return {
        "id": doc.id,
        "filename": file.filename,
        "scope": scope,
        "status": "processing",
        "ingest_job_status": job.status,
    }


@router.post("/link-file", status_code=status.HTTP_202_ACCEPTED)
async def link_file_source(
    body: LinkSourceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    if body.scope not in _ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: {', '.join(_ALLOWED_SCOPES)}",
        )
    try:
        source, sync = await create_linked_source(
            db,
            user_id=current_user.id,
            path=body.path,
            source_type="file",
            scope=body.scope,
            excluded_paths=[],
        )
        source = await get_linked_source(db, source_id=source.id, user_id=current_user.id) or source
        await db.commit()
        return {
            "source": _serialize_linked_source(source),
            "sync": sync,
        }
    except (ValueError, ExtractionError) as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/link-folder", status_code=status.HTTP_202_ACCEPTED)
async def link_folder_source(
    body: LinkSourceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    if body.scope not in _ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: {', '.join(_ALLOWED_SCOPES)}",
        )
    try:
        source, sync = await create_linked_source(
            db,
            user_id=current_user.id,
            path=body.path,
            source_type="folder",
            scope=body.scope,
            excluded_paths=body.excluded_paths,
        )
        source = await get_linked_source(db, source_id=source.id, user_id=current_user.id) or source
        await db.commit()
        return {
            "source": _serialize_linked_source(source),
            "sync": sync,
        }
    except (ValueError, ExtractionError) as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources")
async def list_sources(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    sources = await list_linked_sources(db, user_id=current_user.id)
    return [_serialize_linked_source(source) for source in sources]


@router.get("/sources/{source_id}")
async def get_source_details(
    source_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    source = await get_linked_source(db, source_id=source_id, user_id=current_user.id)
    if source is None:
        raise HTTPException(status_code=404, detail="Linked source not found")
    return {
        "source": _serialize_linked_source(source),
        "tree": await build_linked_source_tree(source),
    }


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: int,
    body: UpdateLinkedSourceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    source = await get_linked_source(db, source_id=source_id, user_id=current_user.id)
    if source is None:
        raise HTTPException(status_code=404, detail="Linked source not found")
    try:
        sync = await update_linked_source_exclusions(
            db,
            source=source,
            user_id=current_user.id,
            excluded_paths=body.excluded_paths,
        )
        source = await get_linked_source(db, source_id=source_id, user_id=current_user.id) or source
        await db.commit()
        return {
            "source": _serialize_linked_source(source),
            "sync": sync,
            "tree": await build_linked_source_tree(source),
        }
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/{source_id}/rescan", status_code=status.HTTP_202_ACCEPTED)
async def rescan_source(
    source_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    source = await get_linked_source(db, source_id=source_id, user_id=current_user.id)
    if source is None:
        raise HTTPException(status_code=404, detail="Linked source not found")
    sync = await rescan_linked_source(db, source=source, user_id=current_user.id)
    source = await get_linked_source(db, source_id=source_id, user_id=current_user.id) or source
    await db.commit()
    return {"source": _serialize_linked_source(source), "sync": sync, "tree": await build_linked_source_tree(source)}


# ── GET /library/query ────────────────────────────────────────────────────────

@router.get("/query")
async def query_library(
    q: str,
    top_k: int = 10,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Semantic search over documents accessible to the current user."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    rag = get_rag_service()
    if not rag.is_ready:
        raise HTTPException(status_code=503, detail="RAG service not ready")

    results = await rag.query(
        query_str=q,
        user_id=current_user.id,
        accessible_scopes=["personal", "family", "shared"],
        top_k=min(top_k, 50),
    )

    return {"query": q, "count": len(results), "results": results}


# ── GET /library/documents ────────────────────────────────────────────────────

@router.get("/documents")
async def list_documents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """
    List documents accessible to the current user.

    Returns a flat array of document objects. Field names match the Swift
    DocumentSummary model (processing_status, created_at).
    """
    result = await db.execute(
        select(Document)
        .options(selectinload(Document.ingest_job))
        .where(
            or_(
                Document.owner_id == current_user.id,
                Document.scope.in_(["family", "shared"]),
            )
        )
        .order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()

    return [_serialize_document(d) for d in docs]


@router.get("/documents/{doc_id}")
async def get_document_details(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return metadata and processing state for one accessible document."""
    result = await db.execute(
        select(Document)
        .options(selectinload(Document.ingest_job))
        .where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _can_access_document(doc, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to access this document")

    return {
        "id": doc.id,
        "filename": doc.original_filename or doc.filename,
        "scope": doc.scope,
        "processing_status": doc.processing_status,
        "content_type": doc.content_type,
        "extraction_method": doc.extraction_method,
        "extracted_text_length": doc.extracted_text_length,
        "chunk_count": doc.chunk_count,
        "summary": doc.summary,
        "error_message": doc.error_message,
        "ingest_job_status": doc.ingest_job.status if doc.ingest_job else None,
        "ingest_attempt_count": doc.ingest_job.attempt_count if doc.ingest_job else 0,
        "ingest_last_error": doc.ingest_job.last_error if doc.ingest_job else None,
        "mime_type": doc.mime_type,
        "file_size_bytes": doc.file_size_bytes,
        "processing_started_at": doc.processing_started_at.isoformat() if doc.processing_started_at else None,
        "processing_completed_at": doc.processing_completed_at.isoformat() if doc.processing_completed_at else None,
        "created_at": doc.created_at.isoformat() if doc.created_at else "",
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else "",
        "source_mode": doc.source_mode,
        "source_sync_status": doc.source_sync_status,
        "linked_source_id": doc.linked_source_id,
        "source_path": doc.file_path,
        "source_last_seen_at": doc.source_last_seen_at.isoformat() if doc.source_last_seen_at else None,
        "source_modified_at": doc.source_modified_at.isoformat() if doc.source_modified_at else None,
    }


@router.get("/documents/{doc_id}/excerpts")
async def get_document_excerpts(
    doc_id: int,
    q: str,
    top_k: int = 8,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return targeted excerpts for one accessible document by query."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _can_access_document(doc, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to access this document")
    if doc.processing_status != "ready":
        raise HTTPException(status_code=409, detail="Document is not ready for excerpts")

    rag = get_rag_service()
    if not rag.is_ready:
        raise HTTPException(status_code=503, detail="RAG service not ready")

    results = await rag.query(
        query_str=q,
        user_id=current_user.id,
        accessible_scopes=["personal", "family", "shared"],
        top_k=min(max(top_k, 1), 50),
    )

    filtered = []
    for row in results:
        md = row.get("metadata") or {}
        if str(md.get("document_id", "")) == str(doc.id):
            filtered.append(
                {
                    "text": row.get("text", ""),
                    "score": row.get("score", 0.0),
                    "metadata": md,
                }
            )

    return {
        "document_id": doc.id,
        "filename": doc.original_filename or doc.filename,
        "query": q,
        "count": len(filtered),
        "results": filtered,
    }


# ── DELETE /library/documents/{id} ───────────────────────────────────────────

@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a document. Only the owner or an admin can delete."""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    is_owner = doc.owner_id == current_user.id
    is_admin = current_user.role in settings.admin_roles

    if not is_owner and not is_admin:
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this document"
        )

    # Remove from vector store (best-effort)
    rag = get_rag_service()
    await rag.delete_document(doc_id)

    if doc.source_mode != "linked":
        try:
            Path(doc.file_path).unlink(missing_ok=True)
        except Exception:
            pass

    await db.delete(doc)


# ── PATCH /library/documents/{id} ────────────────────────────────────────────

class UpdateDocumentRequest(BaseModel):
    scope: str


@router.post("/documents/{doc_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess_document(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    result = await db.execute(
        select(Document)
        .options(selectinload(Document.ingest_job))
        .where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your document")
    rag = get_rag_service()
    await rag.delete_document(doc.id)
    doc.chunk_count = None
    doc.content = None
    doc.summary = None
    doc.extracted_text_length = None
    doc.extraction_method = None
    doc.content_type = None
    job = await enqueue_document_ingest(db, document=doc)
    return {
        "id": doc.id,
        "status": "processing",
        "ingest_job_status": job.status,
    }


@router.patch("/documents/{doc_id}", status_code=200)
async def update_document(
    doc_id: int,
    body: UpdateDocumentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Update document scope. Only the owner can change scope."""
    if body.scope not in _ALLOWED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: {', '.join(_ALLOWED_SCOPES)}",
        )
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your document")
    doc.scope = body.scope
    await db.commit()
    return {"id": doc.id, "scope": doc.scope}


def _can_access_document(doc: Document, user: User) -> bool:
    if doc.owner_id == user.id:
        return True
    return doc.scope in {"family", "shared"}
