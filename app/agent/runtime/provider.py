"""Central model/provider capability resolution for the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.model_provider import is_local_ollama_model, is_native_openai_model
from app.config import settings


def _configured_models(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in str(value or "").split(",") if part.strip())


@dataclass(frozen=True)
class ProviderCapabilities:
    model: str
    family: str
    is_local: bool
    native_streaming: bool
    reasoning_stream: bool
    skip_duplicate_final_stream: bool
    prompt_cache_shape: bool
    prompt_cache_api: bool
    multiple_system_messages: bool
    configured_text_only: bool
    targeted_local_tool_guardrails: bool
    vision: bool
    parallel_tools: bool
    uses_local_api_base: bool
    native_reasoning_effort: str
    tool_mode: str = "enabled"
    allowed_tools: tuple[str, ...] = ()
    blocked_tools: tuple[str, ...] = ()
    runtime_keep_alive: str = ""
    recommended_tool_count_ceiling: int | None = None


def resolve_provider_capabilities(
    model: str | None,
    *,
    reasoning_effort_override: str | None = None,
    include_persisted: bool = True,
) -> ProviderCapabilities:
    """Resolve runtime behavior once instead of branching on model names throughout the loop."""
    selected = str(model or "").strip()
    local = is_local_ollama_model(selected)
    native_openai = is_native_openai_model(selected)
    family = "ollama" if local else "openai" if native_openai else "other"
    native_streaming = bool(
        settings.fruitcake_native_agent_streaming_enabled
        and selected
        and selected in _configured_models(settings.fruitcake_native_agent_streaming_models)
    )
    text_only = bool(
        selected and selected in _configured_models(settings.local_tool_text_only_models)
    )
    targeted_qwen_guardrails = selected == "ollama_chat/qwen3.6:35b"
    tool_ceiling = max(0, int(settings.local_tool_investigation_max_tools or 0))
    profile = None
    if include_persisted:
        from app.model_profiles import get_model_profile_service

        profile = get_model_profile_service().for_model(selected)
    if profile is not None:
        native_streaming = profile.supports_native_streaming
        text_only = profile.tool_mode == "text_only" or not profile.supports_tools
    requested_reasoning = str(reasoning_effort_override or "").strip()
    if profile is not None and requested_reasoning not in profile.reasoning_efforts:
        requested_reasoning = ""
    reasoning_effort = (
        requested_reasoning
        or (profile.default_reasoning_effort if profile is not None else "")
        or (
            str(settings.fruitcake_native_agent_streaming_reasoning_effort or "").strip()
            if native_streaming
            else ""
        )
    )

    return ProviderCapabilities(
        model=selected,
        family=family,
        is_local=local,
        native_streaming=native_streaming,
        reasoning_stream=(profile.supports_thinking if profile is not None else native_streaming),
        skip_duplicate_final_stream=local,
        prompt_cache_shape=local or native_openai,
        prompt_cache_api=native_openai,
        multiple_system_messages=not local,
        configured_text_only=text_only,
        targeted_local_tool_guardrails=targeted_qwen_guardrails,
        vision=(profile.supports_vision if profile is not None else bool(selected and selected == str(settings.image_vision_model or "").strip())),
        parallel_tools=native_openai and (profile.supports_tools if profile is not None else True),
        uses_local_api_base=local or settings.llm_backend in {"ollama", "openai_compat"},
        native_reasoning_effort=reasoning_effort,
        tool_mode=profile.tool_mode if profile is not None else ("text_only" if text_only else "enabled"),
        allowed_tools=profile.allowed_tools if profile is not None and profile.tool_mode == "restricted" else (),
        blocked_tools=profile.blocked_tools if profile is not None else (),
        runtime_keep_alive=profile.keep_alive if profile is not None else "",
        recommended_tool_count_ceiling=(
            tool_ceiling
            if targeted_qwen_guardrails
            and settings.local_tool_investigation_enabled
            and tool_ceiling
            else None
        ),
    )
