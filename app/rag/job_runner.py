"""
FruitcakeAI v5 — Durable document ingest job runner.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Document, DocumentIngestJob
from app.db.session import AsyncSessionLocal
from app.metrics import metrics
from app.rag.document_processor import get_document_processor


log = structlog.get_logger(__name__)

_RETRY_DELAYS_SECONDS = (30, 120, 600)
_runner_task: asyncio.Task | None = None
_runner_active_tasks: set[asyncio.Task] = set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def enqueue_document_ingest(
    db: AsyncSession,
    *,
    document: Document,
    max_attempts: int = 3,
) -> DocumentIngestJob:
    now = _utcnow()
    job = (
        await db.execute(
            select(DocumentIngestJob).where(DocumentIngestJob.document_id == document.id)
        )
    ).scalar_one_or_none()

    document.processing_status = "processing"
    document.error_message = None
    document.processing_started_at = None
    document.processing_completed_at = None

    if job is None:
        job = DocumentIngestJob(
            document_id=document.id,
            status="queued",
            attempt_count=0,
            max_attempts=max_attempts,
            last_error=None,
            queued_at=now,
            next_attempt_at=now,
            started_at=None,
            finished_at=None,
            claimed_at=None,
        )
        db.add(job)
        await db.flush()
        return job

    if job.status in {"queued", "running"}:
        return job

    job.status = "queued"
    job.attempt_count = 0
    job.max_attempts = max_attempts
    job.last_error = None
    job.queued_at = now
    job.next_attempt_at = now
    job.started_at = None
    job.finished_at = None
    job.claimed_at = None
    return job


async def recover_stale_document_ingest_jobs(
    *,
    stale_threshold_minutes: int | None = None,
) -> int:
    age_minutes = int(
        stale_threshold_minutes
        if stale_threshold_minutes is not None
        else settings.document_ingest_stale_running_recovery_minutes
    )
    if age_minutes <= 0:
        return 0

    cutoff = _utcnow() - timedelta(minutes=age_minutes)
    recovered = 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(DocumentIngestJob)
                .options(selectinload(DocumentIngestJob.document))
                .where(DocumentIngestJob.status == "running")
            )
        ).scalars().all()

        for job in rows:
            claimed_at = _normalize_dt(job.claimed_at) or _normalize_dt(job.started_at) or _normalize_dt(job.queued_at)
            if claimed_at is None or claimed_at > cutoff:
                continue

            doc = job.document
            if doc is None:
                continue

            recovered += 1
            metrics.inc_document_ingest_recovered_count()
            if int(job.attempt_count or 0) < int(job.max_attempts or 3):
                job.status = "queued"
                job.last_error = "Recovered interrupted ingest after restart/sleep."
                job.queued_at = _utcnow()
                job.next_attempt_at = _utcnow()
                job.started_at = None
                job.finished_at = None
                job.claimed_at = None
                doc.processing_status = "processing"
                doc.error_message = None
                doc.processing_started_at = None
                doc.processing_completed_at = None
            else:
                job.status = "failed"
                job.last_error = "Processing interrupted — max attempts exceeded."
                job.finished_at = _utcnow()
                job.claimed_at = None
                doc.processing_status = "error"
                doc.error_message = job.last_error
                doc.processing_started_at = None
                doc.processing_completed_at = None

        if recovered:
            await db.commit()
    return recovered


async def start_document_ingest_runner() -> None:
    global _runner_task
    if _runner_task and not _runner_task.done():
        return
    _runner_task = asyncio.create_task(_runner_loop(), name="document_ingest_runner")
    log.info("document.runner_started")


def shutdown_document_ingest_runner() -> None:
    global _runner_task
    if _runner_task and not _runner_task.done():
        _runner_task.cancel()
    for task in list(_runner_active_tasks):
        task.cancel()
    _runner_task = None
    log.info("document.runner_stopped")


async def _runner_loop() -> None:
    max_concurrent = max(1, int(settings.document_ingest_max_concurrent or 1))
    poll_seconds = max(1, int(settings.document_ingest_poll_seconds or 5))
    try:
        while True:
            _prune_active_tasks()
            capacity = max(0, max_concurrent - len(_runner_active_tasks))
            for _ in range(capacity):
                claimed_job_id = await _claim_next_due_job()
                if claimed_job_id is None:
                    break
                task = asyncio.create_task(_run_claimed_job(claimed_job_id))
                _runner_active_tasks.add(task)
                task.add_done_callback(_runner_active_tasks.discard)
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        raise


def _prune_active_tasks() -> None:
    for task in list(_runner_active_tasks):
        if task.done():
            _runner_active_tasks.discard(task)


async def _claim_next_due_job() -> int | None:
    now = _utcnow()
    async with AsyncSessionLocal() as db:
        job = (
            await db.execute(
                select(DocumentIngestJob.id)
                .where(
                    DocumentIngestJob.status == "queued",
                    or_(
                        DocumentIngestJob.next_attempt_at.is_(None),
                        DocumentIngestJob.next_attempt_at <= now,
                    ),
                )
                .order_by(DocumentIngestJob.queued_at.asc(), DocumentIngestJob.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None

        claim = await db.execute(
            update(DocumentIngestJob)
            .where(DocumentIngestJob.id == job, DocumentIngestJob.status == "queued")
            .values(
                status="running",
                attempt_count=DocumentIngestJob.attempt_count + 1,
                started_at=now,
                claimed_at=now,
                finished_at=None,
            )
        )
        if (claim.rowcount or 0) == 0:
            await db.rollback()
            return None
        await db.commit()
        return int(job)


async def _run_claimed_job(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        job = (
            await db.execute(
                select(DocumentIngestJob)
                .options(selectinload(DocumentIngestJob.document))
                .where(DocumentIngestJob.id == job_id)
            )
        ).scalar_one_or_none()
        if job is None or job.document is None:
            return

        doc = job.document
        filename = doc.original_filename or doc.filename
        try:
            await get_document_processor().process(
                db=db,
                document_id=doc.id,
                file_path=Path(doc.file_path),
                user_id=doc.owner_id,
                scope=doc.scope,
                filename=filename,
            )
            job.status = "succeeded"
            job.last_error = None
            job.next_attempt_at = None
            job.finished_at = _utcnow()
            job.claimed_at = None
            await db.commit()
            log.info(
                "document.job_succeeded",
                document_id=doc.id,
                job_id=job.id,
                attempts=job.attempt_count,
            )
        except Exception as exc:
            await db.rollback()
            async with AsyncSessionLocal() as retry_db:
                retry_job = (
                    await retry_db.execute(
                        select(DocumentIngestJob)
                        .options(selectinload(DocumentIngestJob.document))
                        .where(DocumentIngestJob.id == job_id)
                    )
                ).scalar_one_or_none()
                if retry_job is None or retry_job.document is None:
                    return
                await _handle_job_failure(retry_db, retry_job, str(exc))


async def _handle_job_failure(db: AsyncSession, job: DocumentIngestJob, message: str) -> None:
    now = _utcnow()
    doc = job.document
    if doc is None:
        return

    metrics.inc_document_ingest_failed_count()
    attempts = int(job.attempt_count or 0)
    max_attempts = int(job.max_attempts or 3)
    retryable = attempts < max_attempts
    job.last_error = message
    job.claimed_at = None

    if retryable:
        delay = _RETRY_DELAYS_SECONDS[min(attempts - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
        job.status = "queued"
        job.queued_at = now
        job.next_attempt_at = now + timedelta(seconds=delay)
        job.started_at = None
        job.finished_at = None
        doc.processing_status = "processing"
        doc.error_message = None
        doc.processing_started_at = None
        doc.processing_completed_at = None
        log.warning(
            "document.job_requeued",
            document_id=doc.id,
            job_id=job.id,
            attempt_count=attempts,
            next_attempt_at=job.next_attempt_at.isoformat(),
            error=message,
        )
    else:
        job.status = "failed"
        job.next_attempt_at = None
        job.finished_at = now
        doc.processing_status = "error"
        doc.error_message = message
        doc.processing_started_at = None
        doc.processing_completed_at = None
        log.error(
            "document.job_failed",
            document_id=doc.id,
            job_id=job.id,
            attempt_count=attempts,
            error=message,
        )

    await db.commit()
