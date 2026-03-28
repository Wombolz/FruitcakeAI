"""
FruitcakeAI v5 — Agent core loop
LiteLLM-powered tool-calling loop. The LLM drives all orchestration —
it decides when to call tools and how to synthesize results.
"""

from __future__ import annotations

import json
import re
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
    "chat_orchestrated": 15,
}

REPEATED_FAILED_SEARCH_TURN_THRESHOLD = 5
FAILED_SEARCH_PREFIXES = (
    "no results found for:",
    "tool web_search failed:",
)
TASK_ID_RE = re.compile(r'"task_id"\s*:\s*(\d+)')
UNSUPPORTED_ALPHA_VANTAGE_HINT = (
    "I can use Alpha Vantage for quote lookup, daily history, and bounded intraday history right now, "
    "but not weekly, monthly, or technical-indicator endpoints yet. "
    "The current Alpha Vantage adapter supports `global_quote`, `time_series_daily`, and `time_series_intraday`. "
    "If you want, I can fetch a latest quote, recent daily bars, or bounded intraday bars for a symbol."
)


def _build_messages(
    history: List[Dict[str, Any]],
    user_context: UserContext,
) -> List[Dict[str, Any]]:
    """Prepend the system prompt to the conversation history."""
    messages = [{"role": "system", "content": user_context.to_system_prompt()}]
    followup_hint = _recent_task_followup_hint(history)
    if followup_hint:
        messages.append({"role": "system", "content": followup_hint})
    return messages + history


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


def _normalized_local_api_base() -> str:
    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _litellm_kwargs(model: str | None = None) -> Dict[str, Any]:
    """Build extra kwargs for litellm based on the selected model/provider."""
    kwargs: Dict[str, Any] = {}
    selected_model = str(model or settings.llm_model or "")
    if selected_model.startswith(("ollama/", "ollama_chat/")):
        kwargs["api_base"] = _normalized_local_api_base()
        return kwargs
    if settings.llm_backend in ("ollama", "openai_compat"):
        kwargs["api_base"] = _normalized_local_api_base()
    return kwargs


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(((call.get("function") or {}).get("name") or "")).strip()
    return str(getattr(getattr(call, "function", None), "name", "") or "").strip()


def _is_failed_search_turn(tool_calls: List[Any], tool_results: List[Dict[str, Any]]) -> bool:
    if not tool_calls or not tool_results or len(tool_calls) != len(tool_results):
        return False

    for call, result in zip(tool_calls, tool_results):
        tool_name = _tool_call_name(call)
        if tool_name != "web_search":
            return False
        content = str(result.get("content", "")).strip().lower()
        if not any(content.startswith(prefix) for prefix in FAILED_SEARCH_PREFIXES):
            return False

    return True


def _recent_tool_snippets(history: List[Dict[str, Any]], *, limit: int = 3) -> list[str]:
    snippets: list[str] = []
    for message in reversed(history):
        if message.get("role") != "tool":
            continue
        content = " ".join(str(message.get("content", "")).split()).strip()
        if not content:
            continue
        shortened = content[:140] + ("…" if len(content) > 140 else "")
        if shortened not in snippets:
            snippets.append(shortened)
        if len(snippets) >= limit:
            break
    return snippets


def _repeated_failed_search_message(history: List[Dict[str, Any]]) -> str:
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I couldn't reliably finish that lookup after several search attempts. "
            f"Recent results were: {' | '.join(snippets)}. "
            "Try narrowing the request or giving me one item at a time."
        )
    return (
        "I couldn't reliably find enough matching results for that lookup after several search attempts. "
        "Try narrowing the request or giving me one item at a time."
    )


def _max_turns_message(history: List[Dict[str, Any]]) -> str:
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I ran out of turns before I could finish that request cleanly. "
            f"I got as far as: {' | '.join(snippets)}. "
            "Try narrowing the request or splitting it into smaller parts."
        )
    return "I ran out of turns before I could finish that request cleanly. Try narrowing the request."


def _latest_user_message_text(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "") == "user":
            return str(message.get("content") or "")
    return ""


def _extract_recent_task_id(messages: List[Dict[str, Any]]) -> int | None:
    for message in reversed(messages):
        if str(message.get("role") or "") not in {"assistant", "tool"}:
            continue
        content = str(message.get("content") or "")
        match = TASK_ID_RE.search(content)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
    return None


def _recent_task_followup_hint(messages: List[Dict[str, Any]]) -> str | None:
    text = _latest_user_message_text(messages).lower()
    if not text:
        return None
    followup_markers = (
        "run it now",
        "run the task now",
        "change the schedule",
        "change it",
        "update it",
        "modify it",
        "edit it",
        "reschedule",
    )
    if not any(marker in text for marker in followup_markers):
        return None
    task_id = _extract_recent_task_id(messages)
    if task_id is None:
        return None
    return (
        f"Recent task reference: task_id={task_id}. "
        "If the user is asking to modify or run that task, prefer update_task or run_task_now on that task instead of creating a new task."
    )


def _recent_conversation_text(messages: List[Dict[str, Any]], *, limit: int = 6) -> str:
    recent: list[str] = []
    for message in reversed(messages):
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        recent.append(content)
        if len(recent) >= limit:
            break
    return "\n".join(reversed(recent)).lower()


def _unsupported_alphavantage_request_message(messages: List[Dict[str, Any]]) -> str | None:
    text = _latest_user_message_text(messages).lower()
    if not text:
        return None
    conversation_text = _recent_conversation_text(messages)
    provider_in_context = (
        "alpha vantage" in text
        or "alphavantage" in text
        or "alpha vantage" in conversation_text
        or "alphavantage" in conversation_text
    )
    if not provider_in_context:
        return None
    supported_markers = (
        "daily history",
        "daily bars",
        "daily ohlc",
        "daily prices",
        "time_series_daily",
        "last 30 daily",
        "last 5 daily",
        "daily close",
        "daily closes",
        "intraday",
        "intraday bars",
        "intraday ohlc",
        "time_series_intraday",
        "1m",
        "5m",
        "15m",
        "30m",
        "60m",
    )
    if any(marker in text for marker in supported_markers):
        return None

    unsupported_markers = (
        "time_series_weekly",
        "time_series_monthly",
        "weekly",
        "monthly",
        "sma",
        "ema",
        "rsi",
        "macd",
        "bollinger",
        "technical indicator",
    )
    if any(marker in text for marker in unsupported_markers):
        return UNSUPPORTED_ALPHA_VANTAGE_HINT
    return None


def _parse_tool_json_result(content: str) -> Dict[str, Any] | None:
    try:
        payload = json.loads(str(content or "").strip())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _task_handoff_message(
    messages: List[Dict[str, Any]],
    tool_calls: List[Any],
    tool_results: List[Dict[str, Any]],
) -> str | None:
    paired: list[tuple[str, Dict[str, Any] | None]] = []
    for call, result in zip(tool_calls, tool_results):
        tool_name = _tool_call_name(call)
        payload = _parse_tool_json_result(str(result.get("content", "")))
        paired.append((tool_name, payload))

    latest_user = _latest_user_message_text(messages).lower()

    for tool_name, payload in paired:
        if tool_name == "run_task_now" and payload and payload.get("queued") is True:
            title = str(payload.get("title") or "").strip()
            task_id = payload.get("task_id")
            if title:
                return f"Queued task '{title}' (task_id={task_id}) to run now."
            return f"Queued task {task_id} to run now."

    for tool_name, payload in paired:
        if tool_name == "create_and_run_task_plan" and payload and payload.get("run_enqueued") is True:
            task_id = payload.get("task_id")
            return f"Created a plan for task {task_id} and queued it to run now."

    for tool_name, payload in paired:
        if tool_name == "update_task" and payload and payload.get("updated") is True:
            if "run now" in latest_user:
                continue
            title = str(payload.get("title") or "").strip()
            task_id = payload.get("task_id")
            schedule = str(payload.get("schedule") or "").strip()
            suffix = f" Schedule: {schedule}." if schedule else ""
            if title:
                return f"Updated task '{title}' (task_id={task_id}).{suffix}"
            return f"Updated task {task_id}.{suffix}"

    saw_plan = any(
        tool_name in {"create_task_plan", "create_and_run_task_plan"} and payload is not None
        for tool_name, payload in paired
    )
    for tool_name, payload in paired:
        if tool_name == "create_task" and payload and payload.get("created") is True:
            task_type = str(payload.get("task_type") or "").strip().lower()
            title = str(payload.get("title") or "").strip()
            task_id = payload.get("task_id")
            schedule = str(payload.get("schedule") or "").strip()
            profile = str(payload.get("profile") or "").strip()
            if saw_plan or task_type == "recurring" or profile in {"topic_watcher", "iss_pass_watcher", "rss_newspaper", "maintenance", "morning_briefing"}:
                details = []
                if schedule:
                    details.append(f"schedule={schedule}")
                if profile:
                    details.append(f"profile={profile}")
                suffix = f" ({', '.join(details)})" if details else ""
                if title:
                    return f"Created task '{title}' (task_id={task_id}){suffix}."
                return f"Created task {task_id}{suffix}."

    return None


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
    extra = _litellm_kwargs(selected_model)
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
    unsupported_api_message = _unsupported_alphavantage_request_message(messages)
    if unsupported_api_message:
        return unsupported_api_message

    tools = get_tools_for_user(user_context)
    history = list(messages)
    max_turns = TURN_LIMITS.get(mode, 8)
    selected_model = model_override or settings.llm_model
    extra = _litellm_kwargs(selected_model)
    consecutive_failed_search_turns = 0

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
            task_handoff = _task_handoff_message(messages, message.tool_calls, tool_results)
            if task_handoff:
                return task_handoff
            metrics.inc_tool_calls(len(message.tool_calls))
            if _is_failed_search_turn(message.tool_calls, tool_results):
                consecutive_failed_search_turns += 1
                if consecutive_failed_search_turns >= REPEATED_FAILED_SEARCH_TURN_THRESHOLD:
                    log.info(
                        "Stopping repeated failed search loop",
                        turn=turn + 1,
                        model=selected_model,
                        mode=mode,
                        stage=stage,
                    )
                    return _repeated_failed_search_message(history)
            else:
                consecutive_failed_search_turns = 0
            log.info(
                "Tool calls executed",
                turn=turn + 1,
                tools=[_tool_call_name(tc) for tc in message.tool_calls],
                model=selected_model,
                mode=mode,
                stage=stage,
            )
        else:
            # Final text response
            return message.content or ""

    log.warning("Agent hit max turns without a final response", max_turns=max_turns)
    return _max_turns_message(history)


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
    unsupported_api_message = _unsupported_alphavantage_request_message(messages)
    if unsupported_api_message:
        yield unsupported_api_message
        return

    tools = get_tools_for_user(user_context)
    history = list(messages)
    max_turns = TURN_LIMITS.get(mode, 8)
    selected_model = model_override or settings.llm_model
    extra = _litellm_kwargs(selected_model)
    consecutive_failed_search_turns = 0

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
            task_handoff = _task_handoff_message(messages, message.tool_calls, tool_results)
            if task_handoff:
                yield task_handoff
                return
            metrics.inc_tool_calls(len(message.tool_calls))
            if _is_failed_search_turn(message.tool_calls, tool_results):
                consecutive_failed_search_turns += 1
                if consecutive_failed_search_turns >= REPEATED_FAILED_SEARCH_TURN_THRESHOLD:
                    yield _repeated_failed_search_message(history)
                    return
            else:
                consecutive_failed_search_turns = 0
            log.info(
                "Tool calls executed (streaming turn)",
                turn=turn + 1,
                tools=[_tool_call_name(tc) for tc in message.tool_calls],
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

    yield _max_turns_message(history)
