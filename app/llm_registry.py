from __future__ import annotations

from typing import Any, Dict, List

from app.config import settings


def _split_models(raw: str | None) -> List[str]:
    values: List[str] = []
    for item in str(raw or "").split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return values


def _entry(model_id: str, provider: str) -> Dict[str, Any]:
    return {
        "id": model_id,
        "provider": provider,
        "label": model_id,
        "is_default_chat": model_id == settings.llm_model,
        "is_default_task_small": model_id == settings.task_small_model,
        "is_default_task_large": model_id == settings.task_large_model,
    }


def available_llm_models() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_many(provider: str, raw_models: str | None, enabled: bool) -> None:
        if not enabled:
            return
        for model_id in _split_models(raw_models):
            if model_id in seen:
                continue
            seen.add(model_id)
            entries.append(_entry(model_id, provider))

    add_many("openai", settings.openai_models, bool(settings.openai_api_key))
    add_many("anthropic", settings.anthropic_models, bool(settings.anthropic_api_key))
    add_many("gemini", settings.gemini_models, bool(settings.gemini_api_key))
    add_many("local", settings.local_models, bool(settings.local_api_base))

    for fallback_id, provider in (
        (settings.llm_model, "default"),
        (settings.task_small_model, "default"),
        (settings.task_large_model, "default"),
    ):
        model_id = str(fallback_id or "").strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            entries.append(_entry(model_id, provider))

    return entries


def is_configured_model(model_id: str | None) -> bool:
    candidate = str(model_id or "").strip()
    if not candidate:
        return False
    return any(item["id"] == candidate for item in available_llm_models())
