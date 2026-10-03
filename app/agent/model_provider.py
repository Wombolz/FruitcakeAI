"""Small provider-family checks shared by request and usage paths."""

from __future__ import annotations

from app.config import settings


def is_local_ollama_model(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith(("ollama/", "ollama_chat/"))


def is_native_openai_model(model: str | None) -> bool:
    """Identify OpenAI-hosted models without capturing generic compat endpoints."""
    selected = str(model or "").strip().lower()
    if not selected:
        return False
    configured = {
        part.strip().lower()
        for part in str(settings.openai_models or "").split(",")
        if part.strip()
    }
    if selected in configured:
        return True
    candidate = selected.removeprefix("openai/")
    reasoning_family = candidate in {"o1", "o3", "o4"} or any(
        candidate.startswith(f"{family}-") for family in ("o1", "o3", "o4")
    )
    return candidate.startswith(("gpt-", "chatgpt-")) or reasoning_family
