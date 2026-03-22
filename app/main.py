"""
FruitcakeAI v5 — FastAPI Application Entry Point
"""

import logging
import uuid
import structlog
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.session import engine, Base
from app.metrics import metrics

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    log.info("FruitcakeAI v5 starting up", version=settings.app_version)

    # Create all tables if they don't exist yet (Alembic handles production migrations)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    log.info("Database tables ready")

    # Initialize RAG service (loads embedding model + connects to pgvector)
    from app.rag.service import get_rag_service
    await get_rag_service().startup()

    from app.db.session import AsyncSessionLocal
    from app.rag.document_processor import get_document_processor

    async with AsyncSessionLocal() as db:
        recovered = await get_document_processor().recover_stale_documents(db=db)
        if recovered > 0:
            log.warning("Recovered interrupted documents on startup", count=recovered)

    # Initialize MCP registry (connects to enabled Docker servers, loads internal modules)
    from app.mcp.registry import get_mcp_registry
    await get_mcp_registry().startup()

    # Start task scheduler (fires every minute, picks up due tasks)
    from app.autonomy.scheduler import start_scheduler
    await start_scheduler()

    yield

    log.info("FruitcakeAI v5 shutting down")
    from app.autonomy.scheduler import shutdown_scheduler
    shutdown_scheduler()
    await get_mcp_registry().shutdown()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Trace ID middleware ───────────────────────────────────────────────
    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next):
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        # Bind trace_id to structlog for this request
        with structlog.contextvars.bound_contextvars(trace_id=trace_id):
            metrics.inc_requests()
            response = await call_next(request)

        response.headers["X-Trace-ID"] = trace_id
        return response

    # ── Exception handlers — never expose tracebacks to clients ──────────
    from fastapi import HTTPException as FastAPIHTTPException

    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
        trace_id = getattr(request.state, "trace_id", "")
        if exc.status_code >= 500:
            metrics.inc_errors()
            log.error(
                "HTTP error",
                status_code=exc.status_code,
                detail=exc.detail,
                trace_id=trace_id,
                path=str(request.url),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail, "trace_id": trace_id},
            headers={"X-Trace-ID": trace_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        trace_id = getattr(request.state, "trace_id", "")
        metrics.inc_errors()
        log.exception(
            "Unhandled exception",
            trace_id=trace_id,
            path=str(request.url),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "trace_id": trace_id},
            headers={"X-Trace-ID": trace_id},
        )

    # ── Routers (registered as sprints complete) ──────────────────────────
    from app.auth.router import router as auth_router
    from app.api.admin import router as admin_router
    from app.api.library import router as library_router
    from app.api.chat import router as chat_router
    from app.api.tasks import router as tasks_router
    from app.api.devices import router as devices_router
    from app.api.memories import router as memories_router
    from app.api.webhooks import router as webhooks_router
    from app.api.rss import router as rss_router

    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    app.include_router(library_router, prefix="/library", tags=["library"])
    app.include_router(chat_router, prefix="/chat", tags=["chat"])
    app.include_router(tasks_router, tags=["tasks"])
    app.include_router(devices_router, tags=["devices"])
    app.include_router(memories_router, tags=["memories"])
    app.include_router(webhooks_router, tags=["webhooks"])
    app.include_router(rss_router, tags=["rss"])

    return app


app = create_app()


@app.get("/health", tags=["system"])
async def health(request: Request):
    """Quick liveness check — returns 200 when the server is up."""
    return {
        "status": "ok",
        "version": settings.app_version,
        "trace_id": getattr(request.state, "trace_id", ""),
    }
