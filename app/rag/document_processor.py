"""
FruitcakeAI v5 — Document processor lifecycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.metrics import metrics
from app.rag.extractor import DocumentExtractor, ExtractionError
from app.rag.service import get_rag_service


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
            raise ValueError(f"Document {document_id} not found for user {user_id}")

        doc.processing_status = "processing"
        doc.processing_started_at = datetime.now(timezone.utc)
        doc.processing_completed_at = None
        doc.error_message = None
        await db.commit()
        metrics.inc_document_ingest_started_count()

        sink = self._get_sink()
        if not sink.is_ready:
            raise RuntimeError("RAG service not ready")

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
        metrics.inc_document_ingest_succeeded_count()

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

    def _get_sink(self) -> DocumentIndexSink:
        return self._index_sink or get_rag_service()


_processor: DocumentProcessor | None = None


def get_document_processor() -> DocumentProcessor:
    global _processor
    if _processor is None:
        _processor = DocumentProcessor()
    return _processor
