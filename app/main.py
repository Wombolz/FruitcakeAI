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

    # Initialize MCP registry (connects to enabled Docker servers, loads internal modules)
    from app.mcp.registry import get_mcp_registry
    await get_mcp_registry().startup()

    yield

    log.info("FruitcakeAI v5 shutting down")
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

    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    app.include_router(library_router, prefix="/library", tags=["library"])
    app.include_router(chat_router, prefix="/chat", tags=["chat"])

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
