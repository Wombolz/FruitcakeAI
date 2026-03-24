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
