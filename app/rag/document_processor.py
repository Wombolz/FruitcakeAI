"""
FruitcakeAI v5 — Document processor lifecycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.metrics import metrics
from app.rag.extractor import DocumentExtractor, ExtractionError
from app.rag.service import get_rag_service


log = structlog.get_logger(__name__)


@runtime_checkable
class DocumentIndexSink(Protocol):
    async def ingest_text(
        self,
        *,
        text: str,
        document_id: int,
        user_id: int,
        scope: str,
        filename: str,
    ) -> int: ...

    async def delete_document(self, document_id: int) -> None: ...

    @property
    def is_ready(self) -> bool: ...


class DocumentProcessor:
    def __init__(
        self,
        *,
        extractor: DocumentExtractor | None = None,
        index_sink: DocumentIndexSink | None = None,
    ) -> None:
        self._extractor = extractor or DocumentExtractor()
        self._index_sink = index_sink

    async def process(
        self,
        *,
        db: AsyncSession,
        document_id: int,
        file_path: Path,
        user_id: int,
        scope: str,
        filename: str,
    ) -> None:
        doc = await self._get_owned_document(db, document_id, user_id)
        if doc is None:
            return

        doc.processing_status = "processing"
        doc.processing_started_at = datetime.now(timezone.utc)
        doc.processing_completed_at = None
        doc.error_message = None
        await db.commit()
        metrics.inc_document_ingest_started_count()

        sink = self._get_sink()
        if not sink.is_ready:
            await self._mark_error(db, doc, "RAG service not ready")
            return

        try:
            loop = asyncio.get_running_loop()
            method, text = await loop.run_in_executor(None, self._extractor.extract, file_path)
            if not text.strip():
                raise ExtractionError("No text extracted from document")

            doc.content = text
            doc.summary = self._generate_summary(text)
            doc.content_type = self._extractor.content_type_from_extension(file_path)
            doc.extraction_method = method
            doc.extracted_text_length = len(text)
            doc.error_message = None
            await db.commit()

            chunk_count = await sink.ingest_text(
                text=text,
                document_id=document_id,
                user_id=user_id,
                scope=scope,
                filename=filename,
            )

            doc.chunk_count = int(chunk_count)
            doc.processing_status = "ready"
            doc.processing_completed_at = datetime.now(timezone.utc)
            await db.commit()
            log.info(
                "document.ingest_succeeded",
                document_id=document_id,
                filename=filename,
                extraction_method=method,
                chunk_count=int(chunk_count),
            )
            metrics.inc_document_ingest_succeeded_count()
        except Exception as exc:
            await self._mark_error(db, doc, str(exc))
            log.error(
                "document.ingest_failed",
                document_id=document_id,
                filename=filename,
                error=str(exc),
            )
            metrics.inc_document_ingest_failed_count()

    async def reprocess(
        self,
        *,
        db: AsyncSession,
        document_id: int,
        user_id: int,
    ) -> bool:
        doc = await self._get_owned_document(db, document_id, user_id)
        if doc is None:
            return False

        sink = self._get_sink()
        await sink.delete_document(document_id)

        doc.processing_status = "pending"
        doc.error_message = None
        doc.processing_started_at = None
        doc.processing_completed_at = None
        doc.chunk_count = None
        await db.commit()

        await self.process(
            db=db,
            document_id=document_id,
            file_path=Path(doc.file_path),
            user_id=user_id,
            scope=doc.scope,
            filename=doc.original_filename or doc.filename,
        )
        return True

    async def recover_stale_documents(
        self,
        *,
        db: AsyncSession,
        stale_threshold_minutes: int = 15,
    ) -> int:
        rows = (
            await db.execute(
                select(Document).where(Document.processing_status == "processing")
            )
        ).scalars().all()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_threshold_minutes)
        recovered = 0
        for doc in rows:
            started = self._normalize_dt(doc.processing_started_at)
            if started is None or started > cutoff:
                continue
            doc.processing_status = "error"
            doc.error_message = "Processing interrupted — reprocess to retry."
            doc.processing_started_at = None
            doc.processing_completed_at = None
            recovered += 1
            metrics.inc_document_ingest_recovered_count()
            log.warning(
                "document.ingest_recovered",
                document_id=doc.id,
                filename=doc.original_filename or doc.filename,
            )
        if recovered:
            await db.commit()
        return recovered

    def _generate_summary(self, text: str, max_length: int = 300) -> str:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return ""
        sentences = [s.strip() for s in __import__("re").split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
        if not sentences:
            snippet = cleaned[:max_length].rsplit(" ", 1)[0].strip() or cleaned[:max_length].strip()
            return snippet.rstrip(".!?") + "."
        summary_parts: list[str] = []
        current_len = 0
        for sentence in sentences:
            candidate_len = current_len + (1 if summary_parts else 0) + len(sentence)
            if summary_parts and candidate_len > max_length:
                break
            summary_parts.append(sentence)
            current_len = candidate_len
            if current_len >= max_length:
                break
        summary = " ".join(summary_parts).strip() or sentences[0].strip()
        return summary if summary.endswith((".", "!", "?")) else summary + "."

    async def _get_owned_document(
        self,
        db: AsyncSession,
        document_id: int,
        user_id: int,
    ) -> Document | None:
        return (
            await db.execute(
                select(Document).where(Document.id == document_id, Document.owner_id == user_id)
            )
        ).scalar_one_or_none()

    async def _mark_error(self, db: AsyncSession, doc: Document, message: str) -> None:
        doc.processing_status = "error"
        doc.error_message = message
        doc.processing_started_at = None
        doc.processing_completed_at = None
        await db.commit()

    def _get_sink(self) -> DocumentIndexSink:
        return self._index_sink or get_rag_service()

    def _normalize_dt(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


_processor: DocumentProcessor | None = None


def get_document_processor() -> DocumentProcessor:
    global _processor
    if _processor is None:
        _processor = DocumentProcessor()
    return _processor
