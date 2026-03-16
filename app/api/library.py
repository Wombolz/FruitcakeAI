"""
FruitcakeAI v5 — Library API
POST /library/ingest, GET /library/query, GET /library/documents, DELETE /library/documents/{id}
"""

import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import Document, User
from app.db.session import AsyncSessionLocal, get_db
from app.rag.service import get_rag_service

router = APIRouter()

_ALLOWED_SCOPES = {"personal", "family", "shared"}


# ── Background ingest helper ──────────────────────────────────────────────────

async def _ingest_background(
    doc_id: int,
    file_path: Path,
    user_id: int,
    scope: str,
    filename: str,
) -> None:
    """Run RAG embedding after the HTTP response is sent; update processing_status."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if not doc:
            return
        rag = get_rag_service()
        if not rag.is_ready:
            doc.processing_status = "error"
            doc.error_message = "RAG service not ready"
            await db.commit()
            return
        try:
            await rag.ingest(
                file_path=file_path,
                document_id=doc_id,
                user_id=user_id,
                scope=scope,
                filename=filename,
            )
            doc.processing_status = "ready"
        except Exception as e:
            doc.processing_status = "error"
            doc.error_message = str(e)
        await db.commit()


# ── POST /library/ingest ──────────────────────────────────────────────────────

@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(
    background_tasks: BackgroundTasks,
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

    # ── Create DB record (status=processing) ──────────────────────────────────
    doc = Document(
        owner_id=current_user.id,
        filename=safe_name,
        original_filename=file.filename,
        file_path=str(file_path),
        file_size_bytes=file_size,
        mime_type=file.content_type,
        scope=scope,
        processing_status="processing",
        title=file.filename,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)

    # ── Schedule embedding in the background ──────────────────────────────────
    background_tasks.add_task(
        _ingest_background,
        doc_id=doc.id,
        file_path=file_path,
        user_id=current_user.id,
        scope=scope,
        filename=file.filename or safe_name,
    )

    return {
        "id": doc.id,
        "filename": file.filename,
        "scope": scope,
        "status": "processing",
    }


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
        .where(
            or_(
                Document.owner_id == current_user.id,
                Document.scope.in_(["family", "shared"]),
            )
        )
        .order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()

    return [
        {
            "id": d.id,
            "filename": d.original_filename or d.filename,
            "scope": d.scope,
            "created_at": d.created_at.isoformat() if d.created_at else "",
            "processing_status": d.processing_status,
        }
        for d in docs
    ]


@router.get("/documents/{doc_id}")
async def get_document_details(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return metadata and processing state for one accessible document."""
    result = await db.execute(select(Document).where(Document.id == doc_id))
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
        "error_message": doc.error_message,
        "mime_type": doc.mime_type,
        "file_size_bytes": doc.file_size_bytes,
        "created_at": doc.created_at.isoformat() if doc.created_at else "",
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else "",
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

    # Remove file from disk (best-effort)
    try:
        Path(doc.file_path).unlink(missing_ok=True)
    except Exception:
        pass

    await db.delete(doc)


# ── PATCH /library/documents/{id} ────────────────────────────────────────────

class UpdateDocumentRequest(BaseModel):
    scope: str


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
