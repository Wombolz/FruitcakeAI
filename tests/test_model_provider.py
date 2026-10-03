from app.agent.model_provider import is_native_openai_model
from app.config import settings


def test_native_openai_model_detection_covers_configured_and_known_models(monkeypatch):
    monkeypatch.setattr(settings, "openai_models", "custom-openai-model,gpt-5-mini")

    assert is_native_openai_model("gpt-5") is True
    assert is_native_openai_model("openai/gpt-4o") is True
    assert is_native_openai_model("o3") is True
    assert is_native_openai_model("custom-openai-model") is True


def test_native_openai_model_detection_does_not_capture_generic_compat_models(monkeypatch):
    monkeypatch.setattr(settings, "openai_models", "gpt-5")

    assert is_native_openai_model("openai/qwen-local") is False
    assert is_native_openai_model("o365-assistant") is False
    assert is_native_openai_model("ollama_chat/qwen3.6:35b") is False
    assert is_native_openai_model("claude-sonnet-4-6") is False
