"""Track and release Ollama models used by this Fruitcake process."""

from __future__ import annotations

from threading import Lock
from typing import Any

import httpx
import structlog

from app.agent.model_provider import is_local_ollama_model

log = structlog.get_logger(__name__)

_used_models: set[str] = set()
_used_models_lock = Lock()


def _ollama_model_name(model: str | None) -> str:
    candidate = str(model or "").strip()
    if not is_local_ollama_model(candidate):
        return ""
    _, separator, name = candidate.partition("/")
    return name.strip() if separator else ""


def track_local_model_use(model: str | None) -> None:
    """Remember a local model that Fruitcake may have left resident in Ollama."""
    name = _ollama_model_name(model)
    if not name:
        return
    with _used_models_lock:
        _used_models.add(name)


def tracked_local_models() -> tuple[str, ...]:
    with _used_models_lock:
        return tuple(sorted(_used_models))


def clear_tracked_local_models() -> None:
    """Clear process-local tracking; primarily useful for lifecycle isolation."""
    with _used_models_lock:
        _used_models.clear()


def _ollama_api_base(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


async def release_tracked_local_models(
    *,
    api_base: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask Ollama to unload models used by this process without failing shutdown."""
    from app.config import settings

    models = tracked_local_models()
    clear_tracked_local_models()
    report: dict[str, Any] = {
        "requested": len(models),
        "released": [],
        "failed": [],
    }
    if not models:
        return report

    base = _ollama_api_base(api_base or settings.local_api_base)
    if not base:
        report["failed"] = list(models)
        log.warning("local_models.shutdown_release_skipped", reason="missing_api_base", count=len(models))
        return report

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    try:
        for model in models:
            try:
                response = await active_client.post(
                    f"{base}/api/generate",
                    json={"model": model, "keep_alive": 0},
                )
                response.raise_for_status()
                report["released"].append(model)
            except Exception as exc:
                report["failed"].append(model)
                log.warning(
                    "local_model.shutdown_release_failed",
                    model=model,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
    finally:
        if owns_client:
            await active_client.aclose()

    log.info(
        "local_models.shutdown_release_complete",
        requested=report["requested"],
        released=len(report["released"]),
        failed=len(report["failed"]),
    )
    return report
