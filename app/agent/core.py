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
from app.metrics import metrics

log = structlog.get_logger(__name__)

# Silence LiteLLM's verbose request logging in production
litellm.suppress_debug_info = True

# Phase 4: task sessions get more turns for multi-step autonomous work
TURN_LIMITS: Dict[str, int] = {
    "chat": 8,
    "task": 16,
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
    max_turns = 8
    extra = _litellm_kwargs()
    selected_model = model_override or settings.llm_model

    for turn in range(max_turns):
        # Non-streaming for intermediate turns that involve tool calls —
        # we only stream the final text response turn.
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
                mode="chat",
                stage=stage,
            )
            raise

        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        history.append(_normalize_tool_calls(message.model_dump(exclude_none=True)))

        if message.tool_calls:
            tool_results = await dispatch_tool_calls(message.tool_calls, user_context)
            history.extend(tool_results)
            metrics.inc_tool_calls(len(message.tool_calls))
            log.info(
                "Tool calls executed (streaming turn)",
                turn=turn + 1,
                tools=[tc.function.name for tc in message.tool_calls],
                model=selected_model,
                mode="chat",
                stage=stage,
            )
        else:
            # Final turn — yield the already-computed response directly.
            # Re-generating with stream=True risks the LLM paraphrasing or
            # truncating large tool results (e.g. document summaries).
            content = message.content or ""
            CHUNK_SIZE = 64
            for i in range(0, len(content), CHUNK_SIZE):
                yield content[i : i + CHUNK_SIZE]
            return

    yield "I ran into an issue processing your request. Please try again."
