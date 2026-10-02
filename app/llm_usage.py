from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import litellm
import structlog

from app.config import settings
from app.db.models import LLMUsageEvent
from app.db.session import AsyncSessionLocal

log = structlog.get_logger(__name__)

_usage_context: ContextVar[dict[str, Any]] = ContextVar("llm_usage_context", default={})


def bind_llm_usage_context(**values: Any):
    current = dict(_usage_context.get() or {})
    current.update({key: value for key, value in values.items() if value is not None})
    return _usage_context.set(current)


def reset_llm_usage_context(token) -> None:
    _usage_context.reset(token)


def get_llm_usage_context() -> dict[str, Any]:
    return dict(_usage_context.get() or {})


def _extract_usage_counts(response: Any) -> tuple[int, int, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0)
    else:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _extract_local_inference_metrics(response: Any) -> dict[str, int]:
    usage = _value(response, "usage", None)
    if usage is None:
        return {}
    details = _value(usage, "prompt_tokens_details", None)
    metrics = {
        "cached_prompt_tokens": int(
            _value(details, "cached_tokens", _value(usage, "cached_prompt_tokens", 0)) or 0
        )
    }
    provider_fields = _value(response, "provider_specific_fields", {})
    ollama_metrics = provider_fields.get("ollama_metrics", {}) if isinstance(provider_fields, dict) else {}
    for source, target in (
        ("total_duration", "total_duration_ns"),
        ("load_duration", "load_duration_ns"),
        ("prompt_eval_duration", "prompt_eval_duration_ns"),
        ("eval_duration", "eval_duration_ns"),
    ):
        metrics[target] = int(
            (ollama_metrics.get(source) if isinstance(ollama_metrics, dict) else 0)
            or _value(usage, target, 0)
            or 0
        )
    return {key: max(0, value) for key, value in metrics.items()}


def _log_local_inference_metrics(
    response: Any,
    *,
    model: str,
    source: str,
    stage: str | None,
    context: dict[str, Any],
) -> None:
    if not model.lower().startswith(("ollama/", "ollama_chat/")):
        return
    metrics = _extract_local_inference_metrics(response)
    counts = _extract_usage_counts(response)
    if counts is None:
        return
    prompt_tokens = counts[0]
    cached_tokens = metrics.get("cached_prompt_tokens", 0)
    log.info(
        "llm.local_inference_timing",
        model=model,
        source=source,
        stage=stage,
        session_id=context.get("session_id"),
        task_id=context.get("task_id"),
        task_run_id=context.get("task_run_id"),
        prompt_tokens=prompt_tokens,
        cached_prompt_tokens=cached_tokens,
        prompt_cache_percent=round((cached_tokens / prompt_tokens) * 100.0, 2) if prompt_tokens else 0.0,
        total_duration_ms=round(metrics.get("total_duration_ns", 0) / 1_000_000, 2),
        load_duration_ms=round(metrics.get("load_duration_ns", 0) / 1_000_000, 2),
        prompt_eval_duration_ms=round(metrics.get("prompt_eval_duration_ns", 0) / 1_000_000, 2),
        eval_duration_ms=round(metrics.get("eval_duration_ns", 0) / 1_000_000, 2),
    )


def _estimate_cost_usd(response: Any, *, fallback_model: str | None) -> float | None:
    try:
        return float(litellm.completion_cost(completion_response=response))
    except Exception:
        try:
            model = getattr(response, "model", None) or fallback_model
            if not model:
                return None
            return float(
                litellm.completion_cost(
                    completion_response=response,
                    model=model,
                )
            )
        except Exception:
            return None


async def record_llm_usage_event(
    response: Any,
    *,
    source: str | None = None,
    stage: str | None = None,
    user_id: int | None = None,
    session_id: int | None = None,
    task_id: int | None = None,
    task_run_id: int | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    counts = _extract_usage_counts(response)
    if counts is None:
        return

    context = get_llm_usage_context()
    resolved_user_id = int(user_id or context.get("user_id") or 0)
    if resolved_user_id <= 0:
        return

    resolved_model = str(getattr(response, "model", None) or model or context.get("model") or "")
    if not resolved_model:
        return

    prompt_tokens, completion_tokens, total_tokens = counts
    log_context = {
        **context,
        "session_id": session_id if session_id is not None else context.get("session_id"),
        "task_id": task_id if task_id is not None else context.get("task_id"),
        "task_run_id": task_run_id if task_run_id is not None else context.get("task_run_id"),
    }
    _log_local_inference_metrics(
        response,
        model=resolved_model,
        source=str(source or context.get("source") or "llm_call"),
        stage=stage if stage is not None else context.get("stage"),
        context=log_context,
    )
    event = LLMUsageEvent(
        user_id=resolved_user_id,
        session_id=session_id if session_id is not None else context.get("session_id"),
        task_id=task_id if task_id is not None else context.get("task_id"),
        task_run_id=task_run_id if task_run_id is not None else context.get("task_run_id"),
        source=str(source or context.get("source") or "llm_call"),
        stage=stage if stage is not None else context.get("stage"),
        model=resolved_model,
        provider=str(provider or context.get("provider") or settings.llm_backend),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=_estimate_cost_usd(response, fallback_model=resolved_model),
    )

    try:
        async with AsyncSessionLocal() as db:
            db.add(event)
            await db.commit()
    except Exception as exc:
        log.warning(
            "llm_usage.persist_failed",
            error=str(exc),
            source=event.source,
            stage=event.stage,
            model=event.model,
        )


def stream_usage_enabled() -> bool:
    return settings.llm_backend == "openai"
