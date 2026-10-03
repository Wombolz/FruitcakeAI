from app.agent.model_provider import is_native_openai_model
from app.agent.runtime.provider import resolve_provider_capabilities
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


def test_local_qwen_capabilities_resolve_from_config_and_known_override(monkeypatch):
    model = "ollama_chat/qwen3.6:35b"
    monkeypatch.setattr(settings, "fruitcake_native_agent_streaming_enabled", True)
    monkeypatch.setattr(settings, "fruitcake_native_agent_streaming_models", model)
    monkeypatch.setattr(settings, "local_tool_text_only_models", model)
    monkeypatch.setattr(settings, "local_tool_investigation_enabled", True)
    monkeypatch.setattr(settings, "local_tool_investigation_max_tools", 24)
    monkeypatch.setattr(settings, "fruitcake_native_agent_streaming_reasoning_effort", "high")

    profile = resolve_provider_capabilities(model)

    assert profile.family == "ollama"
    assert profile.is_local is True
    assert profile.native_streaming is True
    assert profile.reasoning_stream is True
    assert profile.skip_duplicate_final_stream is True
    assert profile.multiple_system_messages is False
    assert profile.configured_text_only is True
    assert profile.targeted_local_tool_guardrails is True
    assert profile.recommended_tool_count_ceiling == 24
    assert profile.uses_local_api_base is True
    assert profile.native_reasoning_effort == "high"


def test_native_openai_capabilities_do_not_inherit_local_overrides(monkeypatch):
    monkeypatch.setattr(settings, "openai_models", "custom-openai-model")
    monkeypatch.setattr(settings, "local_tool_text_only_models", "")

    profile = resolve_provider_capabilities("custom-openai-model")

    assert profile.family == "openai"
    assert profile.is_local is False
    assert profile.prompt_cache_shape is True
    assert profile.prompt_cache_api is True
    assert profile.multiple_system_messages is True
    assert profile.parallel_tools is True
    assert profile.targeted_local_tool_guardrails is False


def test_generic_provider_capabilities_default_conservatively(monkeypatch):
    monkeypatch.setattr(settings, "openai_models", "")
    monkeypatch.setattr(settings, "local_tool_text_only_models", "")

    profile = resolve_provider_capabilities("anthropic/claude-sonnet")

    assert profile.family == "other"
    assert profile.is_local is False
    assert profile.native_streaming is False
    assert profile.prompt_cache_shape is False
    assert profile.prompt_cache_api is False
    assert profile.parallel_tools is False
    assert resolve_provider_capabilities(None).family == "other"
