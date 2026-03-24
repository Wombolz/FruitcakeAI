from __future__ import annotations

from app.agent.core import _litellm_kwargs
from app.config import settings


def test_litellm_kwargs_uses_local_base_for_ollama_models_even_when_backend_is_openai(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "openai")
    monkeypatch.setattr(settings, "local_api_base", "http://localhost:11434/v1")

    kwargs = _litellm_kwargs("ollama_chat/qwen2.5:14b")

    assert kwargs == {"api_base": "http://localhost:11434"}


def test_litellm_kwargs_does_not_force_local_base_for_openai_models_when_backend_is_openai(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "openai")
    monkeypatch.setattr(settings, "local_api_base", "http://localhost:11434/v1")

    kwargs = _litellm_kwargs("gpt-4o")

    assert kwargs == {}


def test_litellm_kwargs_preserves_existing_ollama_backend_behavior(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "local_api_base", "http://localhost:11434/v1")

    kwargs = _litellm_kwargs("ollama_chat/qwen2.5:32b")

    assert kwargs == {"api_base": "http://localhost:11434"}
