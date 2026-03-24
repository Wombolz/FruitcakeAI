"""
FruitcakeAI v5 — Agent core loop
LiteLLM-powered tool-calling loop. The LLM drives all orchestration —
it decides when to call tools and how to synthesize results.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, List

import litellm
import structlog

from app.agent.context import UserContext
from app.agent.tools import dispatch_tool_calls, get_tools_for_user
from app.config import settings
from app.llm_usage import record_llm_usage_event, stream_usage_enabled
from app.metrics import metrics

log = structlog.get_logger(__name__)

# Silence LiteLLM's verbose request logging in production
litellm.suppress_debug_info = True

# Phase 4: task sessions get more turns for multi-step autonomous work
TURN_LIMITS: Dict[str, int] = {
    "chat": 8,
    "task": 16,
    "chat_orchestrated": 12,
}


def _build_messages(
    history: List[Dict[str, Any]],
    user_context: UserContext,
) -> List[Dict[str, Any]]:
    """Prepend the system prompt to the conversation history."""
    return [{"role": "system", "content": user_context.to_system_prompt()}] + history


def _normalize_tool_calls(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure tool_call arguments are JSON strings, not dicts.
    LiteLLM's model_dump() can deserialize arguments to a dict; re-serialize
    them so the next LiteLLM call doesn't crash in token_counter.
    """
    if not message.get("tool_calls"):
        return message
    fixed = []
    for tc in message["tool_calls"]:
        fn = tc.get("function", {})
        if isinstance(fn.get("arguments"), dict):
            fn = {**fn, "arguments": json.dumps(fn["arguments"])}
            tc = {**tc, "function": fn}
        fixed.append(tc)
    return {**message, "tool_calls": fixed}


def _litellm_kwargs() -> Dict[str, Any]:
    """Build extra kwargs for litellm based on the configured backend."""
    kwargs: Dict[str, Any] = {}
    if settings.llm_backend in ("ollama", "openai_compat"):
        # Strip /v1 suffix — LiteLLM adds the correct path itself
        base = settings.local_api_base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        kwargs["api_base"] = base
    return kwargs


async def _stream_final_response(
    history: List[Dict[str, Any]],
    user_context: UserContext,
    *,
    selected_model: str,
    stage: str | None,
) -> AsyncGenerator[str, None]:
    """
    Stream the final assistant text for a simple chat turn.

    Tools are disabled on this second pass so we do not reopen the tool-calling
    loop after the non-streaming probe determined the turn is a plain text
    response.
    """
    extra = _litellm_kwargs()
    emitted = False

    try:
        stream_kwargs: Dict[str, Any] = {}
        if stream_usage_enabled():
            stream_kwargs["stream_options"] = {"include_usage": True}
        response = await litellm.acompletion(
            model=selected_model,
            messages=_build_messages(history, user_context),
            stream=True,
            **extra,
            **stream_kwargs,
        )
    except Exception as e:
        log.error(
            "LLM streaming call failed",
            error=str(e),
            model=selected_model,
            mode="chat",
            stage=stage,
        )
        raise

    stream_usage_recorded = False
    async for chunk in response:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            await record_llm_usage_event(
                chunk,
                stage=f"{stage}_stream" if stage else "stream_final",
                model=selected_model,
            )
            stream_usage_recorded = True
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if content:
            emitted = True
            yield content

    if not emitted:
        log.warning(
            "LLM streaming completed without token content",
            model=selected_model,
            mode="chat",
            stage=stage,
        )
    if stream_usage_enabled() and not stream_usage_recorded:
        log.info(
            "LLM streaming usage not included in stream response",
            model=selected_model,
            mode="chat",
            stage=stage,
        )


async def run_agent(
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str = "chat",
    model_override: str | None = None,
    stage: str | None = None,
) -> str:
    """
    Run the agent loop (non-streaming).

    Continues calling the LLM until it produces a final text response
    with no pending tool calls.

    Args:
        messages: Conversation history (user/assistant turns, no system message).
        user_context: User identity, persona, and access controls.
        mode: "chat" (default, 8 turns) or "task" (16 turns for autonomous work).

    Returns the assistant's final response as a plain string.
    """
    tools = get_tools_for_user(user_context)
    history = list(messages)
    max_turns = TURN_LIMITS.get(mode, 8)
    extra = _litellm_kwargs()
    selected_model = model_override or settings.llm_model

    for turn in range(max_turns):
        try:
            response = await litellm.acompletion(
                model=selected_model,
                messages=_build_messages(history, user_context),
                tools=tools or None,
                tool_choice="auto" if tools else None,
                **extra,
            )
        except Exception as e:
            log.error("LLM call failed", error=str(e), model=selected_model, mode=mode, stage=stage)
            raise
        await record_llm_usage_event(response, stage=stage, model=selected_model)

        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # Append assistant turn to history (normalize tool_call args to str)
        history.append(_normalize_tool_calls(message.model_dump(exclude_none=True)))

        # Check tool_calls directly — some Ollama models return finish_reason="stop"
        # even when tool calls are present, so we can't rely on finish_reason alone.
        if message.tool_calls:
            # Execute all tool calls, append results, then loop
            tool_results = await dispatch_tool_calls(message.tool_calls, user_context)
            history.extend(tool_results)
            metrics.inc_tool_calls(len(message.tool_calls))
            log.info(
                "Tool calls executed",
                turn=turn + 1,
                tools=[tc.function.name for tc in message.tool_calls],
                model=selected_model,
                mode=mode,
                stage=stage,
            )
        else:
            # Final text response
            return message.content or ""

    log.warning("Agent hit max turns without a final response", max_turns=max_turns)
    return "I ran into an issue processing your request. Please try again."


async def stream_agent(
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str = "chat",
    model_override: str | None = None,
    stage: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Run the agent loop with streaming.

    Yields text tokens as they arrive from the LLM.
    Tool calls are resolved silently; the final response is streamed.
    """
    tools = get_tools_for_user(user_context)
    history = list(messages)
    max_turns = TURN_LIMITS.get(mode, 8)
    extra = _litellm_kwargs()
    selected_model = model_override or settings.llm_model

    for turn in range(max_turns):
        # Probe turn non-streaming so intermediate tool turns stay internal.
        try:
            response = await litellm.acompletion(
                model=selected_model,
                messages=_build_messages(history, user_context),
                tools=tools or None,
                tool_choice="auto" if tools else None,
                stream=False,
                **extra,
            )
        except Exception as e:
            log.error(
                "LLM call failed (streaming turn)",
                error=str(e),
                model=selected_model,
                mode=mode,
                stage=stage,
            )
            raise
        await record_llm_usage_event(
            response,
            stage=f"{stage}_probe" if stage else "stream_probe",
            model=selected_model,
        )

        message = response.choices[0].message

        if message.tool_calls:
            history.append(_normalize_tool_calls(message.model_dump(exclude_none=True)))
            tool_results = await dispatch_tool_calls(message.tool_calls, user_context)
            history.extend(tool_results)
            metrics.inc_tool_calls(len(message.tool_calls))
            log.info(
                "Tool calls executed (streaming turn)",
                turn=turn + 1,
                tools=[tc.function.name for tc in message.tool_calls],
                model=selected_model,
                mode=mode,
                stage=stage,
            )
        else:
            async for token in _stream_final_response(
                history,
                user_context,
                selected_model=selected_model,
                stage=stage,
            ):
                yield token
            return

    yield "I ran into an issue processing your request. Please try again."
