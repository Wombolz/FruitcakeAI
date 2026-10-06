"""
FruitcakeAI v5 — Agent core loop
LiteLLM-powered tool-calling loop. The LLM drives all orchestration —
it decides when to call tools and how to synthesize results.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List

import litellm
import structlog

from app.agent.context import UserContext
from app.agent.context_budget import (
    evidence_class_for_tool,
    plan_request_budget,
    tool_result_char_budget,
)
from app.agent.litellm_ollama_patch import (
    apply_litellm_ollama_stream_patch,
    apply_litellm_ollama_tool_history_patch,
)
from app.agent.model_stream import (
    ModelStreamError,
    ModelTurnAccumulator,
    ModelTurnResult,
    close_provider_stream,
    iter_model_stream_events,
)
from app.agent.runtime import (
    AgentEventEmitter,
    AgentEventType,
    ProviderCapabilities,
    emit_tool_completed_events,
    emit_tool_requested_events,
    normalize_tool_call_results,
    resolve_provider_capabilities,
    wrap_provisional_text_callback,
)
from app.agent.tools import dispatch_tool_calls, get_tools_for_user
from app.autonomy.approval import ApprovalRequired
from app.config import settings
from app.llm_usage import record_llm_usage_event, stream_usage_enabled
from app.metrics import metrics

log = structlog.get_logger(__name__)
_task_handoff_payload: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "task_handoff_payload",
    default=None,
)
_agent_loop_diagnostics: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "agent_loop_diagnostics",
    default={},
)
_agent_runtime_history: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar(
    "agent_runtime_history",
    default=[],
)

# Silence LiteLLM's verbose request logging in production
litellm.suppress_debug_info = True

# litellm's ollama_chat request translation drops assistant tool_calls and
# tool_name from conversation history, corrupting every multi-turn tool
# transcript sent to local models — see app/agent/litellm_ollama_patch.py.
apply_litellm_ollama_tool_history_patch()
apply_litellm_ollama_stream_patch()

# Phase 4: task sessions get more turns for multi-step autonomous work
TURN_LIMITS: Dict[str, int] = {
    "chat": 8,
    "task": 16,
    "chat_orchestrated": 15,
}

REPEATED_FAILED_SEARCH_TURN_THRESHOLD = 5
from app.agent.compaction import (
    CHAT_COMPACTION_BOUNDARY_HEADER,
    RUNTIME_COMPACTION_BOUNDARY_HEADER,
    boundary_message as _shared_boundary_message,
    build_boundary_payload,
    compact_text as _compact_text,
    estimate_history_tokens as _estimate_history_tokens,
    is_compaction_boundary_text,
    recap_summaries,
)
FAILED_SEARCH_PREFIXES = (
    "no results found for:",
    "tool web_search failed:",
)
FAILED_DELETE_PREFIXES = (
    "failed to delete event:",
    "failed to verify deletion for event",
    "deletion requires explicit confirmation.",
)
EXPLORATION_TOOL_NAMES = {
    "list_directory",
    "find_files",
    "read_file",
    "search_code",
    "grep_files",
}
RSS_SEARCH_TOOL_NAMES = {
    "search_my_feeds",
    "search_my_feeds_timeline",
}
RSS_RETRIEVAL_TOOL_NAMES = RSS_SEARCH_TOOL_NAMES | {
    "list_recent_feed_items",
}
DOCUMENT_RETRIEVAL_TOOL_NAMES = {
    "search_library",
    "summarize_document",
}
HEADLINE_ROUNDUP_MARKERS = (
    "headlines this evening",
    "headlines tonight",
    "what are the headlines",
    "what's new right now",
    "whats new right now",
    "top headlines",
    "latest headlines",
    "headline roundup",
    "give me 10 headlines",
    "give me ten headlines",
    "headlines today",
    "today's headlines",
    "todays headlines",
    "round up of",
    "roundup of",
    "news roundup",
    "news round up",
)
HEADLINE_RSS_OWNED_HINTS = (
    "my feeds",
    "my feed",
    "my articles",
    "my article",
    "in my feeds",
    "in my articles",
)
HEADLINE_BROADER_WEB_HINTS = (
    "wider web",
    "across the web",
    "outside my feeds",
    "outside my feed",
    "outside my articles",
    "news sites",
    "from the web",
    "web coverage",
)
RSS_QUERY_FAMILY_STOPWORDS = {
    "a",
    "an",
    "and",
    "any",
    "article",
    "articles",
    "current",
    "evening",
    "feed",
    "feeds",
    "headline",
    "headlines",
    "in",
    "latest",
    "my",
    "news",
    "now",
    "of",
    "on",
    "right",
    "the",
    "this",
    "tonight",
    "top",
    "what",
}
DOCUMENT_QUERY_FAMILY_STOPWORDS = {
    "a",
    "an",
    "and",
    "document",
    "documents",
    "doc",
    "docs",
    "file",
    "files",
    "library",
    "uploaded",
    "the",
    "this",
    "that",
    "more",
    "again",
    "remaining",
    "extract",
    "explain",
    "summary",
    "summarize",
    "section",
    "sections",
    "details",
    "detail",
}
RSS_SYNONYM_NORMALIZATIONS = (
    (r"\bwild[\s-]?fires?\b", "wildfire"),
    (r"\bforest fire(s)?\b", "wildfire"),
    (r"\bbrush fire(s)?\b", "wildfire"),
    (r"\bheadlines?\b", "headline"),
)
TASK_ID_RE = re.compile(r'"task_id"\s*:\s*(\d+)')
UNSUPPORTED_ALPHA_VANTAGE_HINT = (
    "I can use Alpha Vantage for quote lookup, daily history, and bounded intraday history right now, "
    "but not weekly, monthly, or technical-indicator endpoints yet. "
    "The current Alpha Vantage adapter supports `global_quote`, `time_series_daily`, and `time_series_intraday`. "
    "If you want, I can fetch a latest quote, recent daily bars, or bounded intraday bars for a symbol."
)


def reset_agent_loop_diagnostics() -> contextvars.Token:
    return _agent_loop_diagnostics.set({})


def get_agent_loop_diagnostics() -> dict[str, Any]:
    value = _agent_loop_diagnostics.get() or {}
    copied = dict(value)
    if isinstance(value.get("budget_events"), list):
        copied["budget_events"] = [dict(item) if isinstance(item, dict) else item for item in value["budget_events"]]
    if isinstance(value.get("loop_events"), list):
        copied["loop_events"] = [dict(item) if isinstance(item, dict) else item for item in value["loop_events"]]
    return copied


def restore_agent_loop_diagnostics(token: contextvars.Token) -> None:
    _agent_loop_diagnostics.reset(token)


def reset_agent_runtime_history() -> contextvars.Token:
    return _agent_runtime_history.set([])


def get_agent_runtime_history() -> list[dict[str, Any]]:
    return list(_agent_runtime_history.get())


def restore_agent_runtime_history(token: contextvars.Token) -> None:
    _agent_runtime_history.reset(token)


def _build_messages(
    history: List[Dict[str, Any]],
    user_context: UserContext,
    *,
    model: str | None = None,
) -> List[Dict[str, Any]]:
    """Build provider-safe messages while preserving a stable local prefix."""
    capabilities = resolve_provider_capabilities(model)
    followup_hint = _recent_task_followup_hint(history)
    immediate_action_hint = _recent_immediate_action_followup_hint(history)

    if not capabilities.prompt_cache_shape:
        messages = [{"role": "system", "content": user_context.to_system_prompt()}]
        if followup_hint:
            messages.append({"role": "system", "content": followup_hint})
        if immediate_action_hint:
            messages.append({"role": "system", "content": immediate_action_hint})
        return messages + history

    dynamic_parts = [user_context.to_turn_context_prompt()]
    if followup_hint:
        dynamic_parts.append(followup_hint)
    if immediate_action_hint:
        dynamic_parts.append(immediate_action_hint)

    if capabilities.family == "openai":
        messages = [{"role": "system", "content": user_context.to_stable_system_prompt()}]
        turn_context = "\n\n".join(part.strip() for part in dynamic_parts if part and part.strip())
        if turn_context:
            messages.append({"role": "system", "content": turn_context})
        return [*messages, *history]

    provider_history: List[Dict[str, Any]] = []
    for message in history:
        if str(message.get("role") or "") == "system":
            content = str(message.get("content") or "").strip()
            if content:
                dynamic_parts.append(content)
            continue
        provider_history.append(dict(message))

    turn_context = "\n\n".join(part.strip() for part in dynamic_parts if part and part.strip())
    if turn_context:
        for index in range(len(provider_history) - 1, -1, -1):
            if str(provider_history[index].get("role") or "") != "user":
                continue
            current = dict(provider_history[index])
            content = current.get("content")
            envelope = f"<fruitcake_turn_context>\n{turn_context}\n</fruitcake_turn_context>"
            if isinstance(content, list):
                current["content"] = [*content, {"type": "text", "text": envelope}]
            else:
                base = str(content or "").rstrip()
                current["content"] = f"{base}\n\n{envelope}" if base else envelope
            provider_history[index] = current
            break

    return [
        {"role": "system", "content": user_context.to_stable_system_prompt()},
        *provider_history,
    ]


def _request_shape_fingerprints(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
) -> tuple[str, str]:
    """Fingerprint cache-relevant request structure without logging content."""
    stable_prefix = ""
    if messages and str(messages[0].get("role") or "") == "system":
        stable_prefix = str(messages[0].get("content") or "")
    tools_payload = json.dumps(tools or [], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (
        hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()[:16],
        hashlib.sha256(tools_payload.encode("utf-8")).hexdigest()[:16],
    )


def _log_prompt_cache_shape(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    model: str,
    mode: str,
    stage: str | None,
    user_context: UserContext,
    aggressive: bool = False,
) -> None:
    if not resolve_provider_capabilities(model).prompt_cache_shape:
        return
    stable_prefix_fingerprint, tool_schema_fingerprint = _request_shape_fingerprints(messages, tools)
    log.info(
        "agent.prompt_cache_shape",
        model=model,
        mode=mode,
        stage=stage,
        session_id=user_context.session_id,
        task_id=user_context.task_id,
        stable_prefix_fingerprint=stable_prefix_fingerprint,
        tool_schema_fingerprint=tool_schema_fingerprint,
        tool_count=len(tools or []),
        message_count=len(messages),
        recent_roles=[str(message.get("role") or "") for message in messages[-6:]],
        aggressive=aggressive,
    )


def _apply_provider_prompt_cache_kwargs(
    kwargs: Dict[str, Any],
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    model: str,
    user_context: UserContext,
) -> Dict[str, Any]:
    if (
        not settings.openai_prompt_cache_enabled
        or user_context.is_incognito
        or not resolve_provider_capabilities(model).prompt_cache_api
    ):
        return kwargs
    stable_prefix_fingerprint, tool_schema_fingerprint = _request_shape_fingerprints(messages, tools)
    cache_identity = f"{model}|{stable_prefix_fingerprint}|{tool_schema_fingerprint}"
    updated = dict(kwargs)
    updated.setdefault(
        "prompt_cache_key",
        f"fruitcake-{hashlib.sha256(cache_identity.encode('utf-8')).hexdigest()[:32]}",
    )
    retention = str(settings.openai_prompt_cache_retention or "").strip()
    if retention:
        updated.setdefault("prompt_cache_retention", retention)
    return updated


def _sanitize_history_tool_chains(
    history: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    if not history:
        return [], 0

    available_tool_ids: set[str] = set()
    sanitized_reversed: list[Dict[str, Any]] = []
    repaired = 0

    for message in reversed(history):
        role = str(message.get("role") or "").strip()
        current = dict(message)
        current["content"] = str(current.get("content") or "")

        if role == "tool":
            tool_call_id = str(current.get("tool_call_id") or "").strip()
            if tool_call_id:
                available_tool_ids.add(tool_call_id)
            sanitized_reversed.append(current)
            continue

        tool_calls = current.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            required_ids = [tool_call_id for tool_call_id in (_tool_call_id(call) for call in tool_calls) if tool_call_id]
            if required_ids and not all(tool_call_id in available_tool_ids for tool_call_id in required_ids):
                current.pop("tool_calls", None)
                repaired += 1
            elif current.get("tool_calls"):
                current = _normalize_tool_calls(current)

        sanitized_reversed.append(current)

    sanitized_reversed.reverse()
    return sanitized_reversed, repaired


async def _acompletion_with_budget(
    *,
    history: List[Dict[str, Any]],
    user_context: UserContext,
    model: str,
    mode: str,
    stage: str | None,
    stream: bool = False,
    tools: List[Dict[str, Any]] | None = None,
    tool_choice: Any = None,
    extra_kwargs: Dict[str, Any] | None = None,
    stream_kwargs: Dict[str, Any] | None = None,
    event_emitter: AgentEventEmitter | None = None,
) -> Any:
    extra_kwargs = dict(extra_kwargs or {})
    stream_kwargs = dict(stream_kwargs or {})

    initial_messages = _build_messages(history, user_context, model=model)
    initial_budget = plan_request_budget(
        model=model,
        request_messages=initial_messages,
        history=history,
        tools=tools,
    )
    projected_history, report = _project_history_for_model(
        history,
        aggressive=False,
        history_token_limit=initial_budget.history_budget_tokens,
    )
    projected_history, repaired_tool_chains = _sanitize_history_tool_chains(projected_history)
    if repaired_tool_chains:
        log.warning(
            "agent.history_tool_chain_repaired",
            repaired_count=repaired_tool_chains,
            stage=stage,
            mode=mode,
            model=model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
    request_messages = _build_messages(projected_history, user_context, model=model)
    final_budget = plan_request_budget(
        model=model,
        request_messages=request_messages,
        history=projected_history,
        tools=tools,
    )
    report["request_budget"] = final_budget.to_dict()
    _record_budget_event(report, stage=stage, mode=mode, model=model)
    _log_context_budget(
        budget=final_budget.to_dict(),
        report=report,
        model=model,
        mode=mode,
        stage=stage,
        user_context=user_context,
        aggressive=False,
    )
    await _emit_context_budget_event(
        event_emitter,
        budget=final_budget.to_dict(),
        report=report,
        aggressive=False,
    )
    extra_kwargs = _apply_provider_prompt_cache_kwargs(
        extra_kwargs,
        messages=request_messages,
        tools=tools,
        model=model,
        user_context=user_context,
    )
    _log_prompt_cache_shape(
        messages=request_messages,
        tools=tools,
        model=model,
        mode=mode,
        stage=stage,
        user_context=user_context,
    )

    try:
        return await litellm.acompletion(
            model=model,
            messages=request_messages,
            stream=stream,
            tools=tools or None,
            tool_choice=tool_choice if tools else None,
            **extra_kwargs,
            **stream_kwargs,
        )
    except Exception as exc:
        if not settings.agent_overflow_retry_enabled or not _is_context_window_error(exc):
            raise
        aggressive_limit = max(0, initial_budget.history_budget_tokens // 2)
        aggressive_history, aggressive_report = _project_history_for_model(
            history,
            aggressive=True,
            history_token_limit=aggressive_limit,
        )
        aggressive_history, repaired_aggressive_tool_chains = _sanitize_history_tool_chains(aggressive_history)
        if repaired_aggressive_tool_chains:
            log.warning(
                "agent.history_tool_chain_repaired",
                repaired_count=repaired_aggressive_tool_chains,
                stage=stage,
                mode=mode,
                model=model,
                session_id=user_context.session_id,
                task_id=user_context.task_id,
                aggressive=True,
            )
        aggressive_messages = _build_messages(aggressive_history, user_context, model=model)
        aggressive_budget = plan_request_budget(
            model=model,
            request_messages=aggressive_messages,
            history=aggressive_history,
            tools=tools,
        )
        aggressive_report["request_budget"] = aggressive_budget.to_dict()
        _record_budget_event(aggressive_report, stage=stage, mode=mode, model=model)
        _log_context_budget(
            budget=aggressive_budget.to_dict(),
            report=aggressive_report,
            model=model,
            mode=mode,
            stage=stage,
            user_context=user_context,
            aggressive=True,
        )
        await _emit_context_budget_event(
            event_emitter,
            budget=aggressive_budget.to_dict(),
            report=aggressive_report,
            aggressive=True,
        )
        _log_prompt_cache_shape(
            messages=aggressive_messages,
            tools=tools,
            model=model,
            mode=mode,
            stage=stage,
            user_context=user_context,
            aggressive=True,
        )
        try:
            response = await litellm.acompletion(
                model=model,
                messages=aggressive_messages,
                stream=stream,
                tools=tools or None,
                tool_choice=tool_choice if tools else None,
                **extra_kwargs,
                **stream_kwargs,
            )
        except Exception as retry_exc:
            if _is_context_window_error(retry_exc):
                _record_overflow_retry(stage=stage, mode=mode, model=model, succeeded=False)
                raise RuntimeError(
                    "Context budget exceeded after compaction retry. Reduce prompt history or task scope."
                ) from retry_exc
            raise
        _record_overflow_retry(stage=stage, mode=mode, model=model, succeeded=True)
        return response


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
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        if isinstance(fn.get("arguments"), dict):
            fn = {**fn, "arguments": json.dumps(fn["arguments"])}
            tc = {**tc, "function": fn}
        fixed.append(tc)
    return {**message, "tool_calls": fixed}


def _record_agent_runtime_messages(messages: List[Dict[str, Any]]) -> None:
    if not messages:
        return
    current = list(_agent_runtime_history.get())
    current.extend(messages)
    _agent_runtime_history.set(current)


def _normalized_local_api_base() -> str:
    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _litellm_kwargs(model: str | None = None, *, is_incognito: bool = False) -> Dict[str, Any]:
    """Build extra kwargs for litellm based on the selected model/provider."""
    kwargs: Dict[str, Any] = {}
    selected_model = str(model or settings.llm_model or "")
    provider = resolve_provider_capabilities(selected_model)
    if provider.is_local:
        kwargs["api_base"] = _normalized_local_api_base()
        keep_alive = provider.runtime_keep_alive or str(settings.local_model_keep_alive or "").strip()
        if keep_alive and not is_incognito:
            kwargs["keep_alive"] = keep_alive
        return kwargs
    if provider.uses_local_api_base:
        kwargs["api_base"] = _normalized_local_api_base()
    return kwargs


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(((call.get("function") or {}).get("name") or "")).strip()
    return str(getattr(getattr(call, "function", None), "name", "") or "").strip()


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "").strip()
    return str(getattr(call, "id", "") or "").strip()


def _content_fingerprint(value: str, *, length: int = 12) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return "0" * max(1, length)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[: max(1, length)]


def _chunk_plain_text(content: str, chunk_size: int = 64) -> List[str]:
    text = str(content or "")
    if not text:
        return []
    size = max(1, int(chunk_size))
    return [text[i : i + size] for i in range(0, len(text), size)]


def _should_skip_final_stream_pass(model: str) -> bool:
    return resolve_provider_capabilities(model).skip_duplicate_final_stream


def _is_local_model(model: str | None) -> bool:
    return resolve_provider_capabilities(model).is_local


def _native_agent_streaming_enabled(model: str | None) -> bool:
    return resolve_provider_capabilities(model).native_streaming


_REASONING_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
)


def _reasoning_tap_enabled(user_context: UserContext, model: str | None) -> bool:
    return bool(
        settings.fruitcake_local_reasoning_tap
        and _is_local_model(model)
        and not user_context.is_incognito
    )


def _write_reasoning_tap(
    text: str,
    *,
    user_context: UserContext,
    model: str,
    stage: str | None,
    turn: int,
) -> None:
    """Redact a complete turn's reasoning, never individual provider deltas."""
    if not text or not _reasoning_tap_enabled(user_context, model):
        return
    cleaned = text
    for pattern, replacement in _REASONING_SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    prefix = (
        f"\n[fruitcake reasoning session={user_context.session_id or '-'} "
        f"model={model} stage={stage or '-'} turn={turn}]\n"
    )
    sys.stderr.write(prefix + cleaned + "\n")
    sys.stderr.flush()


def _drop_stale_reasoning_content(history: List[Dict[str, Any]]) -> None:
    """Retain streamed reasoning for one immediate replay turn only."""
    for index, message in enumerate(history):
        if str(message.get("role") or "") == "assistant":
            updated = dict(message)
            updated.pop("reasoning_content", None)
            history[index] = updated


def _is_local_tool_json_parse_error(exc: Exception, model: str | None) -> bool:
    if not _is_local_model(model):
        return False
    lowered = str(exc or "").lower()
    return "failed to parse json" in lowered and "ollama" in lowered


def _is_local_tool_unsupported_error(exc: Exception, model: str | None) -> bool:
    if not _is_local_model(model):
        return False
    lowered = str(exc or "").lower()
    return "does not support tools" in lowered and "ollama" in lowered


def _is_configured_local_text_only_model(model: str | None) -> bool:
    return resolve_provider_capabilities(model).configured_text_only


def _is_qwen_local_tool_guardrail_model(model: str | None) -> bool:
    return resolve_provider_capabilities(model).targeted_local_tool_guardrails


def _recent_role_sequence(history: List[Dict[str, Any]], *, limit: int = 5) -> List[str]:
    return [str(message.get("role") or "") for message in history[-max(1, limit):]]


def _sanitize_preview_text(text: str, *, max_chars: int = 180) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)] + "..."


def _tool_names_from_schemas(tools: List[Dict[str, Any]] | None) -> List[str]:
    names: List[str] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str((function or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _is_browserish_tool_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return False
    browser_tokens = (
        "browser",
        "playwright",
        "page",
        "navigate",
        "click",
        "type",
        "tab",
        "dom",
        "selector",
        "screenshot",
    )
    return any(token in normalized for token in browser_tokens)


def _tool_surface_summary(tools: List[Dict[str, Any]] | None) -> Dict[str, Any]:
    tool_names = _tool_names_from_schemas(tools)
    browser_tools = [name for name in tool_names if _is_browserish_tool_name(name)]
    return {
        "offered_tools": tool_names,
        "offered_tool_count": len(tool_names),
        "offered_browser_tools": browser_tools,
        "offered_browser_tool_count": len(browser_tools),
        "offered_has_browser_tools": bool(browser_tools),
    }


def _tool_message_preview(history: List[Dict[str, Any]], *, limit: int = 5) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for message in history[-max(1, limit):]:
        role = str(message.get("role") or "")
        row: Dict[str, Any] = {"role": role}
        content = str(message.get("content") or "")
        if content:
            row["content_preview"] = _sanitize_preview_text(content)
        if role == "assistant":
            tool_calls = list(message.get("tool_calls") or [])
            if tool_calls:
                row["tool_call_names"] = [_tool_call_name(call) for call in tool_calls]
                row["tool_call_count"] = len(tool_calls)
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id:
                row["tool_call_id"] = tool_call_id
        preview.append(row)
    return preview


def _history_prompt_fingerprint(history: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for message in history[-5:]:
        role = str(message.get("role") or "")
        content = _sanitize_preview_text(str(message.get("content") or ""), max_chars=120)
        parts.append(f"{role}:{content}")
    return _content_fingerprint(" | ".join(parts), length=16)


def _raw_error_preview(exc: Exception) -> str:
    for attr_name in ("body", "response", "llm_provider_response", "text"):
        value = getattr(exc, attr_name, None)
        if not value:
            continue
        return _sanitize_preview_text(str(value), max_chars=240)
    return _sanitize_preview_text(str(exc), max_chars=240)


def _local_tool_failure_phase(history: List[Dict[str, Any]]) -> str:
    saw_tool_result = any(str(message.get("role") or "") == "tool" for message in history)
    if saw_tool_result:
        return "after_successful_tool_turn"
    saw_tool_call = any(
        str(message.get("role") or "") == "assistant" and bool(message.get("tool_calls"))
        for message in history
    )
    if saw_tool_call:
        return "after_emitted_tool_calls"
    return "initial_tool_enabled_turn"


def _local_tool_prompt_class(history: List[Dict[str, Any]]) -> str:
    latest_user = _latest_user_message_text(history).lower()
    system_notes = "\n".join(
        str(message.get("content") or "")
        for message in history
        if str(message.get("role") or "") == "system"
    ).lower()

    if _is_narrow_document_fact_lookup(latest_user):
        return "document_fact_lookup"
    if (
        "recent workspace file context for this chat session:" in system_notes
        or (
            "workspace" in latest_user
            and any(token in latest_user for token in ("repo map", "report", "file", "document", "notes", "key points"))
            and any(token in latest_user for token in ("latest", "recent", "just", "working on", "tell me about", "what is in"))
        )
    ):
        return "workspace_followup"
    if "required grounding for this turn: this is a library intent." in system_notes:
        return "library_grounding"
    return "general"


def _is_narrow_document_fact_lookup(latest_user: str) -> bool:
    text = str(latest_user or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in ("summarize", "summary", "overview", "recap")):
        return False
    doc_markers = ("manual", "document", "pdf", "user guide", "operation manual")
    fact_markers = (
        "default",
        "ip address",
        "address",
        "port",
        "setting",
        "parameter",
        "what does",
        "what is",
        "which",
        "where",
        "page",
        "say",
    )
    return any(marker in text for marker in doc_markers) and any(marker in text for marker in fact_markers)


_LOCAL_SUMMARY_DIGEST_MAX_CHARS = 2200
_LOCAL_SUMMARY_DIGEST_MAX_FINDINGS = 6


def build_local_document_summary_digest(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return "Document summary digest unavailable."

    document_name = _extract_summary_header_value(r"Summary of '([^']+)'", text)
    total_sections = _extract_summary_header_value(r"\((\d+)\s+total sections\)", text)
    coverage_note = _extract_summary_header_value(r"_([^_]*?Note:[^_]*)_", text)

    section_lines: list[str] = []
    finding_lines: list[str] = []
    caveat_lines: list[str] = []
    generic_lines: list[str] = []
    active_bucket = "generic"

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("**Summary of '") or line.startswith("_Note:"):
            continue

        normalized_heading = line.lstrip("#").strip().lower() if line.startswith("#") else ""
        if normalized_heading:
            if "major sections" in normalized_heading or "section overview" in normalized_heading:
                active_bucket = "sections"
                continue
            if "key findings" in normalized_heading or "observed facts" in normalized_heading:
                active_bucket = "findings"
                continue
            if "caveats" in normalized_heading or "uncertainty" in normalized_heading:
                active_bucket = "caveats"
                continue
            active_bucket = "generic"

        if line.startswith(("-", "*")):
            normalized = _sanitize_preview_text(line.lstrip("-* ").strip(), max_chars=180)
            if not normalized:
                continue
            lower = normalized.lower()
            if "recommend" in lower or "should " in lower or "next step" in lower:
                continue
            if active_bucket == "sections":
                if normalized not in section_lines:
                    section_lines.append(normalized)
                continue
            if active_bucket == "caveats" or "caveat" in lower or "uncertain" in lower:
                if normalized not in caveat_lines:
                    caveat_lines.append(normalized)
                continue
            if active_bucket == "findings" or len(finding_lines) < _LOCAL_SUMMARY_DIGEST_MAX_FINDINGS:
                if normalized not in finding_lines:
                    finding_lines.append(normalized)
                continue
        if line.startswith(("###", "####")):
            normalized = _sanitize_preview_text(line.lstrip("#").strip(), max_chars=140)
            if normalized and normalized not in section_lines:
                section_lines.append(normalized)
            continue

        normalized = _sanitize_preview_text(line, max_chars=180)
        if not normalized:
            continue
        lower = normalized.lower()
        if ("note:" in lower or "sample" in lower or "coverage" in lower or "uncertain" in lower) and normalized not in caveat_lines:
            caveat_lines.append(normalized)
            continue
        if len(generic_lines) < _LOCAL_SUMMARY_DIGEST_MAX_FINDINGS and normalized not in generic_lines:
            generic_lines.append(normalized)

    if not section_lines:
        section_lines = generic_lines[:3]
    if not finding_lines:
        finding_lines = generic_lines[:_LOCAL_SUMMARY_DIGEST_MAX_FINDINGS]
    if not caveat_lines and coverage_note:
        caveat_lines = [_sanitize_preview_text(coverage_note, max_chars=180)]
    if not finding_lines:
        finding_lines = [_sanitize_preview_text(text, max_chars=180)]

    lines = ["Document summary evidence digest."]
    if document_name:
        lines.append(f"- Document: {document_name}")
    if total_sections:
        lines.append(f"- Total sections: {total_sections}")
    if coverage_note:
        lines.append(f"- Coverage note: {coverage_note}")
    lines.append("- Major sections:")
    for section in section_lines[:4]:
        lines.append(f"  - {section}")
    lines.append("- Key findings:")
    for finding in finding_lines[:_LOCAL_SUMMARY_DIGEST_MAX_FINDINGS]:
        lines.append(f"  - {finding}")
    if caveat_lines:
        lines.append("- Caveats:")
        for caveat in caveat_lines[:3]:
            lines.append(f"  - {caveat}")
    return "\n".join(lines)


def _latest_large_summarize_document_tool_result(history: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not history:
        return None
    tool_lookup = _tool_name_lookup(history)
    message = history[-1]
    if str(message.get("role") or "") != "tool":
        return None
    tool_call_id = str(message.get("tool_call_id") or "").strip()
    if tool_lookup.get(tool_call_id) != "summarize_document":
        return None
    content = str(message.get("content") or "")
    if len(content) < _LOCAL_SUMMARY_DIGEST_MAX_CHARS:
        return None
    return message


def _extract_summary_header_value(pattern: str, content: str) -> str:
    match = re.search(pattern, content, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _summarize_document_tool_result_for_local_synthesis(content: str) -> str:
    return build_local_document_summary_digest(content)


def _apply_local_document_summary_guardrail(
    *,
    history: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    model: str,
    mode: str,
    stage: str | None,
    user_context: UserContext,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]] | None]:
    if mode not in {"chat", "chat_orchestrated"} or not _is_qwen_local_tool_guardrail_model(model):
        return history, tools

    target = _latest_large_summarize_document_tool_result(history)
    if target is None:
        return history, tools

    compacted_history = list(history)
    compacted_tool = dict(target)
    compacted_tool["content"] = _summarize_document_tool_result_for_local_synthesis(str(target.get("content") or ""))
    compacted_history[-1] = compacted_tool
    note = (
        "A large summarize_document result is already available for this turn. "
        "Use the compact document-summary evidence already in the conversation as the source of truth and answer the user's summary request directly. "
        "Stay close to the evidence, avoid unsupported specifics, and if a detail is missing or unclear, say so briefly instead of inferring it. "
        "Do not call more tools. If the user needs a narrower section, say that briefly."
    )
    compacted_history = _insert_system_note_before_latest_user(compacted_history, note)
    _log_local_tool_event(
        event="LLM local_tool_guardrail_applied",
        history=history,
        tools=tools,
        model=model,
        mode=mode,
        stage=stage,
        user_context=user_context,
        prompt_class="document_summary_followup",
        tool_failure_phase="guardrail_large_document_summary_post_tool_synthesis",
        tools_enabled=False,
    )
    return compacted_history, None


def _local_tool_guardrail(history: List[Dict[str, Any]], *, model: str | None, mode: str) -> Dict[str, Any] | None:
    if mode in {"chat", "chat_orchestrated"} and _is_configured_local_text_only_model(model):
        return {
            "prompt_class": "configured_text_only_model",
            "instruction": (
                "This local model is configured as text-only in Fruitcake. "
                "Do not call tools. Answer using only the existing conversation context and any grounding already present. "
                "If fresh tool access would be required, say that briefly instead of inventing details."
            ),
        }
    if mode not in {"chat", "chat_orchestrated"} or not _is_qwen_local_tool_guardrail_model(model):
        return None
    prompt_class = _local_tool_prompt_class(history)
    if prompt_class == "document_fact_lookup":
        return {
            "prompt_class": prompt_class,
            "instruction": (
                "This local model is unreliable when narrow manual/document fact lookups escalate into full-document summaries. "
                "If tools are needed, prefer search_library excerpts and answer directly from those excerpts. "
                "Do not call summarize_document unless the user explicitly asked for a summary or the excerpts are clearly insufficient."
            ),
            "allowed_tool_names": ["search_library", "list_library_documents"],
        }
    if prompt_class == "workspace_followup":
        return {
            "prompt_class": prompt_class,
            "instruction": (
                "This local model is unreliable at tool calling for workspace follow-up turns. "
                "Do not call tools. Answer using only the existing conversation context and any workspace grounding already present. "
                "If you cannot confirm the file contents from existing context, say that clearly and briefly."
            ),
        }
    if prompt_class == "library_grounding":
        return {
            "prompt_class": prompt_class,
            "instruction": (
                "This local model is unreliable at tool calling for already-grounded library turns. "
                "Do not call tools. Answer using only the grounding already present in the conversation. "
                "If the grounding is insufficient, say that briefly instead of inventing details."
            ),
        }
    return None


def _build_tool_parse_fallback_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    note = (
        "Tool calling failed for this local model on this turn. "
        "Do not call tools. Answer using only the existing conversation context and any grounding already present. "
        "If the answer would require fresh tool access, say that briefly instead of inventing details."
    )
    return _insert_system_note_before_latest_user(history, note)


def _apply_local_tool_guardrail(
    *,
    history: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    model: str,
    mode: str,
    stage: str | None,
    user_context: UserContext,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]] | None]:
    turn_history = history
    turn_tools = tools
    turn_history, turn_tools = _apply_local_document_summary_guardrail(
        history=turn_history,
        tools=turn_tools,
        model=model,
        mode=mode,
        stage=stage,
        user_context=user_context,
    )
    guardrail = _local_tool_guardrail(history, model=model, mode=mode)
    if not turn_tools or guardrail is None:
        return turn_history, turn_tools

    turn_history = _insert_system_note_before_latest_user(history, str(guardrail["instruction"]))
    allowed_tool_names = [str(name).strip() for name in (guardrail.get("allowed_tool_names") or []) if str(name).strip()]
    if allowed_tool_names:
        filtered = [
            tool for tool in turn_tools
            if str(((tool or {}).get("function") or {}).get("name") or "").strip() in set(allowed_tool_names)
        ]
        turn_tools = filtered or None
        _log_local_tool_event(
            event="LLM local_tool_guardrail_applied",
            history=history,
            tools=tools,
            model=model,
            mode=mode,
            stage=stage,
            user_context=user_context,
            prompt_class=str(guardrail.get("prompt_class") or ""),
            tool_failure_phase="guardrail_restrict_tools",
            tools_enabled=bool(turn_tools),
        )
        return turn_history, turn_tools

    turn_tools = None
    _log_local_tool_event(
        event="LLM local_tool_guardrail_applied",
        history=history,
        tools=tools,
        model=model,
        mode=mode,
        stage=stage,
        user_context=user_context,
        prompt_class=str(guardrail.get("prompt_class") or ""),
        tool_failure_phase="guardrail_preemptive_text_only",
        tools_enabled=False,
    )
    return turn_history, turn_tools


def _apply_local_tool_investigation_filters(
    *,
    tools: List[Dict[str, Any]] | None,
    model: str,
    mode: str,
    stage: str | None,
    user_context: UserContext,
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]] | None:
    if not tools or not settings.local_tool_investigation_enabled or not _is_qwen_local_tool_guardrail_model(model):
        return tools

    filtered = list(tools)
    reasons: List[str] = []

    if settings.local_tool_investigation_drop_browser_tools:
        browser_filtered = [
            tool
            for tool in filtered
            if not _is_browserish_tool_name(str(((tool or {}).get("function") or {}).get("name") or ""))
        ]
        if len(browser_filtered) != len(filtered):
            filtered = browser_filtered
            reasons.append("drop_browser_tools")

    max_tools = max(0, int(settings.local_tool_investigation_max_tools or 0))
    if max_tools and len(filtered) > max_tools:
        filtered = filtered[:max_tools]
        reasons.append(f"cap_tools:{max_tools}")

    if reasons:
        _log_local_tool_event(
            event="LLM local_tool_investigation_filter_applied",
            history=history,
            tools=filtered,
            model=model,
            mode=mode,
            stage=stage,
            user_context=user_context,
            prompt_class=_local_tool_prompt_class(history),
            tool_failure_phase="investigation_filter",
            tools_enabled=bool(filtered),
            raw_tool_surface=_tool_surface_summary(tools),
            effective_tool_surface=_tool_surface_summary(filtered),
            investigation_reasons=reasons,
        )

    return filtered or None


def _insert_system_note_before_latest_user(history: List[Dict[str, Any]], note: str) -> List[Dict[str, Any]]:
    updated = list(history)
    if updated and updated[-1].get("role") == "user":
        return updated[:-1] + [{"role": "system", "content": note}, updated[-1]]
    return updated + [{"role": "system", "content": note}]


def _log_local_tool_event(
    *,
    event: str,
    history: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    model: str | None,
    mode: str,
    stage: str | None,
    user_context: UserContext,
    error: Exception | None = None,
    prompt_class: str | None = None,
    tool_failure_phase: str | None = None,
    tools_enabled: bool = True,
    raw_tool_surface: Dict[str, Any] | None = None,
    effective_tool_surface: Dict[str, Any] | None = None,
    investigation_reasons: List[str] | None = None,
) -> None:
    tool_surface = _tool_surface_summary(tools)
    payload = {
        "model": str(model or ""),
        "mode": mode,
        "stage": stage or "",
        "session_id": user_context.session_id,
        "task_id": user_context.task_id,
        "tools_enabled": tools_enabled,
        **tool_surface,
        "recent_roles": _recent_role_sequence(history),
        "prompt_fingerprint": _content_fingerprint(_latest_user_message_text(history), length=16),
        "history_fingerprint": _history_prompt_fingerprint(history),
        "prompt_class": prompt_class or _local_tool_prompt_class(history),
        "tool_failure_phase": tool_failure_phase or _local_tool_failure_phase(history),
        "history_preview": _tool_message_preview(history),
    }
    if error is not None:
        payload["error_preview"] = _raw_error_preview(error)
    if raw_tool_surface is not None:
        payload["raw_tool_surface"] = raw_tool_surface
    if effective_tool_surface is not None:
        payload["effective_tool_surface"] = effective_tool_surface
    if investigation_reasons:
        payload["investigation_reasons"] = investigation_reasons
    log.warning(event, **payload)


def _tool_name_lookup(history: List[Dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for message in history:
        for call in message.get("tool_calls") or []:
            call_id = _tool_call_id(call)
            tool_name = _tool_call_name(call)
            if call_id and tool_name:
                lookup[call_id] = tool_name
    return lookup


def _compact_tool_message(
    message: Dict[str, Any],
    *,
    tool_name_lookup: dict[str, str],
    max_chars: int,
) -> Dict[str, Any]:
    content = str(message.get("content") or "")
    tool_call_id = str(message.get("tool_call_id") or "").strip()
    tool_name = tool_name_lookup.get(tool_call_id) or "unknown_tool"
    evidence_class = evidence_class_for_tool(tool_name)
    compact_summary = _compact_structured_catalog(content, max_chars=max_chars)
    if compact_summary is None:
        compact_summary = (
            _compact_evidence_text(content, max_chars=max_chars)
            if evidence_class != "ordinary"
            else _compact_text(content, max_chars=max_chars)
        )
    compacted = (
        "Compacted tool result.\n"
        f"Tool: {tool_name}\n"
        f"Evidence class: {evidence_class}\n"
        f"Tool call id: {tool_call_id or 'unknown'}\n"
        f"Fingerprint: {_content_fingerprint(content)}\n"
        f"Original chars: {len(content)}\n"
        f"Summary: {compact_summary}"
    )
    return {**message, "content": compacted}


def _compact_evidence_text(content: str, *, max_chars: int) -> str:
    """Keep both source framing and trailing citations when evidence is reduced."""
    text = str(content or "").strip()
    if len(text) <= max_chars:
        return text
    marker = "\n\n[... middle evidence omitted by context budget ...]\n\n"
    available = max(0, max_chars - len(marker))
    head_chars = int(available * 0.7)
    tail_chars = available - head_chars
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}"


_CATALOG_IDENTITY_FIELDS = (
    "id",
    "name",
    "title",
    "label",
    "slug",
    "key",
    "type",
    "status",
    "description",
    "capabilities",
)


def _compact_structured_catalog(content: str, *, max_chars: int) -> str | None:
    """Compact JSON catalogs without dropping entries from the middle or tail."""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    list_fields = [(key, value) for key, value in payload.items() if isinstance(value, list)]
    if len(list_fields) != 1 or not list_fields[0][1]:
        return None
    field_name, entries = list_fields[0]

    projected: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            projected.append(entry)
            continue
        summary: dict[str, Any] = {}
        for key in _CATALOG_IDENTITY_FIELDS:
            value = entry.get(key)
            if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                summary[key] = value
            elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
                summary[key] = value
        if not summary:
            for key, value in entry.items():
                if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                    summary[key] = value
                if len(summary) >= 3:
                    break
        projected.append(summary)

    compact_payload = {
        field_name: projected,
        "_compaction": {
            "all_entries_retained": True,
            "entry_count": len(entries),
            "detail_fields_omitted": True,
        },
    }
    rendered = json.dumps(compact_payload, ensure_ascii=True, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return rendered

    identity_only: list[Any] = []
    for entry in projected:
        if not isinstance(entry, dict):
            identity_only.append(entry)
            continue
        identity = {
            key: entry[key]
            for key in ("id", "name", "title", "label", "slug", "key", "type")
            if key in entry
        }
        identity_only.append(identity or entry)
    compact_payload[field_name] = identity_only
    rendered = json.dumps(compact_payload, ensure_ascii=True, separators=(",", ":"))
    return rendered if len(rendered) <= max_chars else None


def _is_compaction_boundary_message(message: Dict[str, Any]) -> bool:
    if str(message.get("role") or "") != "system":
        return False
    return is_compaction_boundary_text(str(message.get("content") or ""))


def _build_compaction_boundary_message(
    prefix: List[Dict[str, Any]],
    *,
    tool_name_lookup: dict[str, str],
) -> Dict[str, Any]:
    recap = recap_summaries(
        prefix,
        head_count=3,
        tail_count=7,
        max_lines=10,
        tool_name_lookup=tool_name_lookup,
    )
    return _shared_boundary_message(build_boundary_payload(mode="runtime", recap=recap))


def _snap_cut_to_tool_chain(history: List[Dict[str, Any]], cut_index: int) -> int:
    """Move a prefix/suffix cut backwards so a suffix never starts with tool
    results whose assistant tool-call message landed in the prefix."""
    while 0 < cut_index < len(history) and str(history[cut_index].get("role") or "") == "tool":
        cut_index -= 1
    return cut_index


def _project_history_for_model(
    history: List[Dict[str, Any]],
    *,
    aggressive: bool = False,
    history_token_limit: int | None = None,
    tool_result_max_chars: int | None = None,
) -> tuple[List[Dict[str, Any]], dict[str, Any]]:
    effective_history_limit = (
        max(0, int(history_token_limit))
        if history_token_limit is not None
        else int(settings.agent_history_soft_token_limit)
    )
    effective_tool_result_max_chars = (
        max(400, int(tool_result_max_chars))
        if tool_result_max_chars is not None
        else int(settings.agent_tool_result_max_chars)
    )
    projected = list(history)
    report: dict[str, Any] = {
        "aggressive": aggressive,
        "history_token_limit": effective_history_limit,
        "tool_result_max_chars": effective_tool_result_max_chars,
        "estimated_tokens_before": _estimate_history_tokens(history),
        "estimated_tokens_after": 0,
        "tool_results_compacted": 0,
        "tool_compactions": [],
        "compaction_boundary_applied": False,
        "boundary_messages_collapsed": 0,
        "boundary_messages_preserved": 0,
    }
    tool_lookup = _tool_name_lookup(history)
    tool_indices = [index for index, message in enumerate(projected) if str(message.get("role") or "") == "tool"]
    keep_recent_tools = max(0, int(settings.agent_tool_recent_keep))
    recent_tool_indices = set(tool_indices[-keep_recent_tools:]) if keep_recent_tools else set()
    for index in tool_indices:
        message = projected[index]
        content = str(message.get("content") or "")
        if not content:
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        tool_name = tool_lookup.get(tool_call_id) or "unknown_tool"
        result_limit = (
            effective_tool_result_max_chars
            if aggressive
            else tool_result_char_budget(
                tool_name,
                history_budget_tokens=effective_history_limit,
                ordinary_max_chars=effective_tool_result_max_chars,
            )
        )
        compaction_limit = result_limit
        should_compact = aggressive or len(content) > compaction_limit
        if not should_compact and index not in recent_tool_indices:
            compaction_limit = max(400, result_limit // 2)
            should_compact = len(content) > compaction_limit
        if not should_compact:
            continue
        compacted_message = _compact_tool_message(
            message,
            tool_name_lookup=tool_lookup,
            max_chars=compaction_limit,
        )
        retained_content = str(compacted_message.get("content") or "")
        if len(retained_content) >= len(content):
            continue
        projected[index] = compacted_message
        report["tool_results_compacted"] += 1
        report["tool_compactions"].append(
            {
                "tool": tool_name,
                "evidence_class": evidence_class_for_tool(tool_name),
                "original_chars": len(content),
                "retained_chars": len(retained_content),
                "limit_chars": compaction_limit,
            }
        )

    estimated_after = _estimate_history_tokens(projected)
    keep_recent_messages = max(1, int(settings.agent_recent_messages_keep))
    if projected and (aggressive or estimated_after > effective_history_limit):
        cut = _snap_cut_to_tool_chain(projected, max(0, len(projected) - keep_recent_messages))
        prefix = projected[:cut]
        suffix = projected[cut:]
        if prefix:
            pinned_boundaries = [message for message in prefix if _is_compaction_boundary_message(message)]
            collapsible = [message for message in prefix if not _is_compaction_boundary_message(message)]
            if collapsible:
                projected = (
                    pinned_boundaries
                    + [_build_compaction_boundary_message(collapsible, tool_name_lookup=tool_lookup)]
                    + suffix
                )
                report["compaction_boundary_applied"] = True
                report["boundary_messages_collapsed"] = len(collapsible)
            else:
                projected = pinned_boundaries + suffix
            report["boundary_messages_preserved"] = len(pinned_boundaries)
            estimated_after = _estimate_history_tokens(projected)

    report["estimated_tokens_after"] = estimated_after
    return projected, report


def _log_context_budget(
    *,
    budget: dict[str, Any],
    report: dict[str, Any],
    model: str,
    mode: str,
    stage: str | None,
    user_context: UserContext,
    aggressive: bool,
) -> None:
    log_method = log.warning if budget.get("over_budget") else log.info
    log_method(
        "agent.context_budget",
        model=model,
        mode=mode,
        stage=stage,
        session_id=user_context.session_id,
        task_id=user_context.task_id,
        policy_source=budget.get("policy_source"),
        context_window_tokens=budget.get("context_window_tokens"),
        usable_input_tokens=budget.get("usable_input_tokens"),
        estimated_input_tokens=budget.get("estimated_input_tokens"),
        history_tokens=budget.get("history_tokens"),
        history_budget_tokens=budget.get("history_budget_tokens"),
        fixed_message_tokens=budget.get("fixed_message_tokens"),
        tool_schema_tokens=budget.get("tool_schema_tokens"),
        estimated_headroom_tokens=budget.get("estimated_headroom_tokens"),
        tool_results_compacted=report.get("tool_results_compacted"),
        compaction_boundary_applied=report.get("compaction_boundary_applied"),
        aggressive=aggressive,
    )


async def _emit_context_budget_event(
    emitter: AgentEventEmitter | None,
    *,
    budget: dict[str, Any],
    report: dict[str, Any],
    aggressive: bool,
) -> None:
    if emitter is None or not emitter.is_observed:
        return
    await emitter.emit(
        AgentEventType.CONTEXT_BUDGET,
        model=budget.get("model"),
        policy_source=budget.get("policy_source"),
        context_window_tokens=budget.get("context_window_tokens"),
        output_reserve_tokens=budget.get("output_reserve_tokens"),
        reasoning_reserve_tokens=budget.get("reasoning_reserve_tokens"),
        safety_margin_tokens=budget.get("safety_margin_tokens"),
        usable_input_tokens=budget.get("usable_input_tokens"),
        estimated_input_tokens=budget.get("estimated_input_tokens"),
        history_tokens=budget.get("history_tokens"),
        history_budget_tokens=budget.get("history_budget_tokens"),
        tool_schema_tokens=budget.get("tool_schema_tokens"),
        estimated_headroom_tokens=budget.get("estimated_headroom_tokens"),
        tool_results_compacted=report.get("tool_results_compacted"),
        tool_compactions=list(report.get("tool_compactions") or [])[:8],
        aggressive=aggressive,
    )


def _record_budget_event(report: dict[str, Any], *, stage: str | None, mode: str, model: str) -> None:
    if not report:
        return
    current = dict(_agent_loop_diagnostics.get() or {})
    events = list(current.get("budget_events") or [])
    event = {
        "stage": stage or "",
        "mode": mode,
        "model": model,
        **report,
    }
    events.append(event)
    current["budget_events"] = events[-20:]
    current["tool_results_compacted"] = int(current.get("tool_results_compacted") or 0) + int(report.get("tool_results_compacted") or 0)
    current["compaction_boundaries"] = int(current.get("compaction_boundaries") or 0) + (
        1 if report.get("compaction_boundary_applied") else 0
    )
    current["max_estimated_tokens_before"] = max(
        int(current.get("max_estimated_tokens_before") or 0),
        int(report.get("estimated_tokens_before") or 0),
    )
    current["max_estimated_tokens_after"] = max(
        int(current.get("max_estimated_tokens_after") or 0),
        int(report.get("estimated_tokens_after") or 0),
    )
    _agent_loop_diagnostics.set(current)


def _record_overflow_retry(*, stage: str | None, mode: str, model: str, succeeded: bool) -> None:
    current = dict(_agent_loop_diagnostics.get() or {})
    current["overflow_retries"] = int(current.get("overflow_retries") or 0) + 1
    current["overflow_retry_succeeded"] = bool(current.get("overflow_retry_succeeded") or succeeded)
    events = list(current.get("budget_events") or [])
    events.append(
        {
            "stage": stage or "",
            "mode": mode,
            "model": model,
            "overflow_retry": True,
            "overflow_retry_succeeded": succeeded,
        }
    )
    current["budget_events"] = events[-20:]
    _agent_loop_diagnostics.set(current)


def _record_loop_event(*, event_type: str, stage: str | None, mode: str, model: str, details: dict[str, Any]) -> None:
    current = dict(_agent_loop_diagnostics.get() or {})
    events = list(current.get("loop_events") or [])
    events.append(
        {
            "type": event_type,
            "stage": stage or "",
            "mode": mode,
            "model": model,
            **details,
        }
    )
    current["loop_events"] = events[-20:]
    _agent_loop_diagnostics.set(current)


def _is_context_window_error(exc: Exception) -> bool:
    lowered = str(exc or "").lower()
    markers = (
        "contextwindowexceedederror",
        "input tokens exceed",
        "maximum context length",
        "prompt is too long",
        "messages resulted in",
        "context length exceeded",
    )
    return any(marker in lowered for marker in markers)


def _turn_state_summary(history: List[Dict[str, Any]], *, limit: int = 6) -> Dict[str, Any]:
    recent_roles = [str(item.get("role") or "") for item in history[-limit:]]
    recent_tools = [
        str(item.get("tool_call_id") or "")
        for item in history[-limit:]
        if str(item.get("role") or "") == "tool"
    ]
    last_user = _latest_user_message_text(history)
    return {
        "history_len": len(history),
        "recent_roles": recent_roles,
        "recent_tool_call_ids": recent_tools[-3:],
        "last_user_fingerprint": _content_fingerprint(last_user),
    }


def _tool_result_fingerprints(tool_results: List[Dict[str, Any]]) -> List[str]:
    return [
        _content_fingerprint(str(result.get("content", "")))
        for result in tool_results
    ]


def _tool_call_signature(tool_calls: List[Any], tool_results: List[Dict[str, Any]]) -> str:
    names = [_tool_call_name(call) or "unknown" for call in tool_calls]
    result_fingerprints = _tool_result_fingerprints(tool_results)
    combined = "|".join(f"{name}:{fingerprint}" for name, fingerprint in zip(names, result_fingerprints))
    return combined or "no_tool_signature"


def _tool_call_arguments(call: Any) -> Dict[str, Any]:
    if isinstance(call, dict):
        raw = ((call.get("function") or {}).get("arguments"))
    else:
        raw = getattr(getattr(call, "function", None), "arguments", None)
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _semantic_tool_signature(tool_calls: List[Any]) -> str | None:
    if len(tool_calls) != 1:
        return None
    tool_name = _tool_call_name(tool_calls[0])
    arguments = _tool_call_arguments(tool_calls[0])
    if tool_name in DOCUMENT_RETRIEVAL_TOOL_NAMES:
        target = str(
            arguments.get("document_name")
            or arguments.get("query")
            or arguments.get("filename")
            or ""
        ).strip().lower()
        if not target:
            return None
        family = _normalize_document_query_family(target)
        return f"{tool_name}:{family}"
    if tool_name not in RSS_SEARCH_TOOL_NAMES:
        return None
    ignored_keys = {
        "refresh",
        "max_results",
        "max_total_results",
        "max_results_per_day",
    }
    normalized = {
        key: value
        for key, value in arguments.items()
        if key not in ignored_keys
    }
    if tool_name == "search_my_feeds":
        normalized.pop("days_back", None)
    try:
        payload = json.dumps(normalized, ensure_ascii=True, sort_keys=True)
    except Exception:
        payload = str(normalized)
    return f"{tool_name}:{payload}"


def _normalize_document_query_family(query: str) -> str:
    text = str(query or "").strip().lower()
    if not text:
        return "document"
    raw_tokens = re.findall(r"[a-z0-9_.-]+", text)
    tokens: list[str] = []
    for token in raw_tokens:
        if token in DOCUMENT_QUERY_FAMILY_STOPWORDS:
            continue
        tokens.append(token)
    if not tokens:
        return text
    return "|".join(dict.fromkeys(tokens))


def _normalize_query_family(query: str) -> str:
    text = str(query or "").strip().lower()
    if not text:
        return "latest"
    for pattern, replacement in RSS_SYNONYM_NORMALIZATIONS:
        text = re.sub(pattern, replacement, text)
    raw_tokens = re.findall(r"[a-z0-9]+", text)
    tokens: list[str] = []
    for token in raw_tokens:
        if token in RSS_QUERY_FAMILY_STOPWORDS:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("es") and len(token) > 4:
            token = token[:-2]
        elif token.endswith("s") and len(token) > 3:
            token = token[:-1]
        if token and token not in RSS_QUERY_FAMILY_STOPWORDS:
            tokens.append(token)
    if not tokens:
        return "latest"
    return "|".join(sorted(dict.fromkeys(tokens)))


def _rss_query_family_signature(tool_calls: List[Any]) -> str | None:
    signatures: list[str] = []
    for call in tool_calls:
        tool_name = _tool_call_name(call)
        if tool_name not in RSS_RETRIEVAL_TOOL_NAMES:
            continue
        arguments = _tool_call_arguments(call)
        if tool_name == "search_my_feeds_timeline":
            query_family = _normalize_query_family(str(arguments.get("query") or ""))
            start = str(arguments.get("start_date") or "")
            end = str(arguments.get("end_date") or "")
            signatures.append(f"{tool_name}:{query_family}:{start}:{end}")
            continue
        if tool_name == "search_my_feeds":
            query_family = _normalize_query_family(str(arguments.get("query") or ""))
            category = str(arguments.get("category") or "")
            signatures.append(f"{tool_name}:{query_family}:{category}")
            continue
        sources = arguments.get("sources") or {}
        window = arguments.get("window") or {}
        source_mode = str((sources.get("mode") or "all")).strip().lower()
        window_mode = str((window.get("mode") or "all")).strip().lower()
        signatures.append(f"{tool_name}:{source_mode}:{window_mode}")
    if not signatures:
        return None
    return " || ".join(dict.fromkeys(signatures))


def _is_headline_roundup_prompt(messages: List[Dict[str, Any]]) -> bool:
    text = _latest_user_message_text(messages).lower()
    if not text:
        return False
    return any(marker in text for marker in HEADLINE_ROUNDUP_MARKERS)


def _is_rss_owned_headline_prompt(messages: List[Dict[str, Any]]) -> bool:
    text = _latest_user_message_text(messages).lower()
    if not text or not _is_headline_roundup_prompt(messages):
        return False
    if any(marker in text for marker in HEADLINE_BROADER_WEB_HINTS):
        return False
    return True


WEB_CONTEXT_RESEARCH_MARKERS = (
    "across sources",
    "analyze",
    "analysis",
    "compare",
    "comparison",
    "comprehensive",
    "deep dive",
    "detailed",
    "evidence",
    "how has",
    "in-depth",
    "investigate",
    "latest developments",
    "research",
    "synthesize",
    "what changed",
)
WEB_CONTEXT_DEEP_MARKERS = (
    "comprehensive",
    "deep dive",
    "detailed",
    "in-depth",
    "thorough",
)


def _prompt_benefits_from_web_context(messages: List[Dict[str, Any]]) -> bool:
    """Reserve provider context for prompts that ask for multi-source synthesis."""
    text = _latest_user_message_text(messages).casefold()
    return bool(text and any(marker in text for marker in WEB_CONTEXT_RESEARCH_MARKERS))


def _web_context_depth_for_prompt(messages: List[Dict[str, Any]]) -> str:
    text = _latest_user_message_text(messages).casefold()
    return "deep" if any(marker in text for marker in WEB_CONTEXT_DEEP_MARKERS) else "standard"


def _apply_web_context_first_turn_policy(
    tools: List[Dict[str, Any]] | None,
    *,
    turn_number: int,
    enabled: bool,
) -> tuple[List[Dict[str, Any]] | None, Any]:
    if not enabled or turn_number != 1 or not tools:
        return tools, "auto"
    context_tools = [
        tool
        for tool in tools
        if str(((tool.get("function") or {}).get("name") or "")).strip() == "web_context"
    ]
    if not context_tools:
        return tools, "auto"
    return context_tools, {"type": "function", "function": {"name": "web_context"}}


def _rewrite_web_context_tool_calls(
    tool_calls: List[Dict[str, Any]],
    *,
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize the forced context call so provider depth follows user intent."""
    rewritten: List[Dict[str, Any]] = []
    depth = _web_context_depth_for_prompt(messages)
    fallback_query = _latest_user_message_text(messages).strip()
    for call in tool_calls:
        if _tool_call_name(call) != "web_context":
            rewritten.append(call)
            continue
        arguments = _tool_call_arguments(call)
        arguments["depth"] = depth
        if not str(arguments.get("query") or "").strip() and fallback_query:
            arguments["query"] = fallback_query[:600]
        function = dict(call.get("function") or {})
        function["arguments"] = json.dumps(arguments)
        rewritten.append({**call, "function": function})
    return rewritten


def _filter_tools_for_prompt(
    tools: List[Dict[str, Any]],
    *,
    messages: List[Dict[str, Any]],
    rss_owned_headline_prompt: bool,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
) -> List[Dict[str, Any]]:
    filtered = list(tools)
    if not _prompt_benefits_from_web_context(messages):
        filtered = [
            tool
            for tool in filtered
            if str(((tool.get("function") or {}).get("name") or "")).strip() != "web_context"
        ]
    if not rss_owned_headline_prompt or mode not in {"chat", "chat_orchestrated"}:
        return filtered
    without_web_search = [
        tool
        for tool in filtered
        if str(((tool.get("function") or {}).get("name") or "")).strip() != "web_search"
    ]
    if len(without_web_search) != len(filtered):
        log.info(
            "agent.headline_roundup_rss_lane",
            skipped_web_search=True,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
    return without_web_search


def _rss_result_item_count(content: str) -> int:
    text = str(content or "")
    return len(re.findall(r"\[\d+\]", text))


def _is_empty_rss_result(content: str) -> bool:
    lowered = str(content or "").strip().lower()
    empty_markers = (
        "no results found for",
        "no cached results found for",
        "no timeline results found for",
        "no recent cached headlines found",
        "no recent feed items found",
        "no active rss sources available",
    )
    return any(marker in lowered for marker in empty_markers)


def _recent_rss_evidence(history: List[Dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    tool_lookup = _tool_name_lookup(history)
    evidence: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    for message in reversed(history):
        if str(message.get("role") or "") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        tool_name = tool_lookup.get(tool_call_id) or ""
        if tool_name not in RSS_RETRIEVAL_TOOL_NAMES:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        fingerprint = _content_fingerprint(content)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        evidence.append(
            {
                "tool_name": tool_name,
                "content": content,
                "item_count": _rss_result_item_count(content),
                "is_empty": _is_empty_rss_result(content),
            }
        )
        if len(evidence) >= limit:
            break
    return list(reversed(evidence))


def _is_empty_document_result(content: str) -> bool:
    lowered = str(content or "").strip().lower()
    empty_markers = (
        "no matching documents found",
        "no documents found",
        "no excerpts found",
        "no relevant excerpts found",
        "could not find a document",
        "multiple documents match",
    )
    return any(marker in lowered for marker in empty_markers)


def _recent_document_evidence(history: List[Dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    tool_lookup = _tool_name_lookup(history)
    arguments_lookup: dict[str, Dict[str, Any]] = {}
    for message in history:
        for call in message.get("tool_calls") or []:
            call_id = _tool_call_id(call)
            if call_id:
                arguments_lookup[call_id] = _tool_call_arguments(call)
    evidence: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    for message in reversed(history):
        if str(message.get("role") or "") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        tool_name = tool_lookup.get(tool_call_id) or ""
        if tool_name not in DOCUMENT_RETRIEVAL_TOOL_NAMES:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        fingerprint = _content_fingerprint(content)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        arguments = arguments_lookup.get(tool_call_id) or {}
        target = str(
            arguments.get("document_name")
            or arguments.get("query")
            or arguments.get("filename")
            or ""
        ).strip()
        evidence.append(
            {
                "tool_name": tool_name,
                "target": target,
                "content": content,
                "is_empty": _is_empty_document_result(content),
            }
        )
        if len(evidence) >= limit:
            break
    return list(reversed(evidence))


def _history_contains_document_tool_activity(history: List[Dict[str, Any]]) -> bool:
    tool_lookup = _tool_name_lookup(history)
    for message in history:
        for call in message.get("tool_calls") or []:
            if _tool_call_name(call) in DOCUMENT_RETRIEVAL_TOOL_NAMES:
                return True
        if str(message.get("role") or "") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_lookup.get(tool_call_id) in DOCUMENT_RETRIEVAL_TOOL_NAMES:
            return True
    return False


def _document_evidence_summary(evidence: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(evidence, start=1):
        target = str(item.get("target") or "").strip()
        label = f"{item.get('tool_name')}"
        if target:
            label += f", target={target}"
        lines.append(f"Evidence block {index} ({label}):\n{str(item.get('content') or '').strip()}")
    return "\n\n".join(lines).strip()


def _has_nonempty_document_summary_evidence(history: List[Dict[str, Any]]) -> bool:
    evidence = _recent_document_evidence(history, limit=6)
    for item in evidence:
        if str(item.get("tool_name") or "") != "summarize_document":
            continue
        if not bool(item.get("is_empty")):
            return True
    return False


def _history_contains_rss_tool_activity(history: List[Dict[str, Any]]) -> bool:
    tool_lookup = _tool_name_lookup(history)
    for message in history:
        for call in message.get("tool_calls") or []:
            if _tool_call_name(call) in RSS_RETRIEVAL_TOOL_NAMES:
                return True
        if str(message.get("role") or "") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_lookup.get(tool_call_id) in RSS_RETRIEVAL_TOOL_NAMES:
            return True
    return False


def _rss_evidence_summary(evidence: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(evidence, start=1):
        content = str(item.get("content") or "").strip()
        if item.get("tool_name") == "list_recent_feed_items":
            filtered_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("Summary:"):
                    continue
                filtered_lines.append(line)
            content = "\n".join(filtered_lines).strip()
        lines.append(
            f"Evidence block {index} ({item.get('tool_name')}, item_count={int(item.get('item_count') or 0)}):\n{content}"
        )
    return "\n\n".join(lines).strip()


def _rewrite_headline_rss_tool_calls(
    tool_calls: List[Dict[str, Any]],
    *,
    rss_owned_headline_prompt: bool,
    prior_recent_feed_fetches: int,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
) -> tuple[List[Dict[str, Any]], int]:
    if not rss_owned_headline_prompt or mode not in {"chat", "chat_orchestrated"}:
        return tool_calls, prior_recent_feed_fetches

    rewritten: List[Dict[str, Any]] = []
    downgraded = 0
    for call in tool_calls:
        if _tool_call_name(call) != "list_recent_feed_items":
            rewritten.append(call)
            continue
        call_id = _tool_call_id(call)
        arguments = dict(_tool_call_arguments(call))
        if prior_recent_feed_fetches > 0 and bool(arguments.get("refresh", False)):
            arguments["refresh"] = False
            downgraded += 1
            function = dict(call.get("function") or {})
            function["arguments"] = json.dumps(arguments)
            rewritten.append({**call, "function": function})
        else:
            rewritten.append(call)
        prior_recent_feed_fetches += 1

    if downgraded:
        log.info(
            "agent.headline_roundup_refresh_downgraded",
            downgraded_count=downgraded,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
    return rewritten, prior_recent_feed_fetches


def _rss_evidence_is_thin(evidence: list[dict[str, Any]]) -> bool:
    if not evidence:
        return True
    total_items = sum(int(item.get("item_count") or 0) for item in evidence)
    non_empty_blocks = sum(1 for item in evidence if not item.get("is_empty"))
    return total_items <= 1 or non_empty_blocks <= 1


async def _synthesize_from_rss_evidence(
    *,
    history: List[Dict[str, Any]],
    user_context: UserContext,
    selected_model: str,
    mode: str,
    stage: str | None,
    reason: str,
) -> str | None:
    evidence = _recent_rss_evidence(history)
    if not evidence:
        if _history_contains_rss_tool_activity(history):
            log.warning(
                "agent.rss_evidence_missing_from_history",
                reason=reason,
                mode=mode,
                stage=stage,
                model=selected_model,
                session_id=user_context.session_id,
                task_id=user_context.task_id,
            )
        return None
    latest_user = _latest_user_message_text(history).strip()
    if not latest_user:
        return None
    evidence_summary = _rss_evidence_summary(evidence)
    if not evidence_summary:
        return None
    thin = _rss_evidence_is_thin(evidence)
    instruction = (
        "You already searched the user's RSS feeds. Do not call more tools. "
        "Answer the user's last RSS/news question directly using only the evidence below. "
        "If the evidence is weak or noisy, say that clearly and briefly instead of pretending there is stronger coverage."
        if thin
        else
        "You already searched the user's RSS feeds. Do not call more tools. "
        "Answer the user's last RSS/news question directly using only the evidence below. "
        "Synthesize the strongest relevant items and keep the answer grounded."
    )
    reason_line = (
        "The feed search was starting to loop through reformulations instead of converging."
        if reason == "rss_query_family_churn"
        else "The feed search should stop here and synthesize from the evidence already gathered."
    )
    synthesis_history = [
        message
        for message in history
        if str(message.get("role") or "") in {"user", "assistant"}
    ][-4:]
    synthesis_history.append(
        {
            "role": "user",
            "content": (
                f"{instruction}\n\n"
                f"Original request:\n{latest_user}\n\n"
                f"Why you must answer now:\n{reason_line}\n\n"
                f"RSS evidence:\n{evidence_summary}"
            ),
        }
    )
    extra = _litellm_kwargs(selected_model, is_incognito=user_context.is_incognito)
    response = await _acompletion_with_budget(
        history=synthesis_history,
        user_context=user_context,
        model=selected_model,
        mode=mode,
        stage=f"{stage}_rss_synthesis" if stage else "rss_synthesis",
        extra_kwargs=extra,
    )
    await record_llm_usage_event(
        response,
        stage=f"{stage}_rss_synthesis" if stage else "rss_synthesis",
        model=selected_model,
    )
    message = response.choices[0].message
    return str(message.content or "").strip() or None


async def _safe_synthesize_from_rss_evidence(
    *,
    history: List[Dict[str, Any]],
    user_context: UserContext,
    selected_model: str,
    mode: str,
    stage: str | None,
    reason: str,
) -> str | None:
    try:
        return await _synthesize_from_rss_evidence(
            history=history,
            user_context=user_context,
            selected_model=selected_model,
            mode=mode,
            stage=stage,
            reason=reason,
        )
    except Exception as exc:
        log.warning(
            "agent.rss_synthesis_failed",
            error=str(exc),
            reason=reason,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
        return None


async def _synthesize_from_document_evidence(
    *,
    history: List[Dict[str, Any]],
    user_context: UserContext,
    selected_model: str,
    mode: str,
    stage: str | None,
    reason: str,
) -> str | None:
    evidence = _recent_document_evidence(history)
    if not evidence:
        if _history_contains_document_tool_activity(history):
            log.warning(
                "agent.document_evidence_missing_from_history",
                reason=reason,
                mode=mode,
                stage=stage,
                model=selected_model,
                session_id=user_context.session_id,
                task_id=user_context.task_id,
            )
        return None
    latest_user = _latest_user_message_text(history).strip()
    if not latest_user:
        return None
    evidence_summary = _document_evidence_summary(evidence)
    if not evidence_summary:
        return None
    instruction = (
        "You already retrieved library/document evidence. Do not call more tools. "
        "Answer the user's last document question directly using only the evidence below. "
        "If the evidence is partial, say what is known and what remains unresolved."
    )
    reason_line = (
        "The document lookup was repeating the same target instead of converging."
        if reason == "document_query_family_churn"
        else "The document workflow should stop here and synthesize from the evidence already gathered."
    )
    synthesis_history = [
        message
        for message in history
        if str(message.get("role") or "") in {"user", "assistant"}
    ][-4:]
    synthesis_history.append(
        {
            "role": "user",
            "content": (
                f"{instruction}\n\n"
                f"Original request:\n{latest_user}\n\n"
                f"Why you must answer now:\n{reason_line}\n\n"
                f"Document evidence:\n{evidence_summary}"
            ),
        }
    )
    extra = _litellm_kwargs(selected_model, is_incognito=user_context.is_incognito)
    response = await _acompletion_with_budget(
        history=synthesis_history,
        user_context=user_context,
        model=selected_model,
        mode=mode,
        stage=f"{stage}_document_synthesis" if stage else "document_synthesis",
        extra_kwargs=extra,
    )
    await record_llm_usage_event(
        response,
        stage=f"{stage}_document_synthesis" if stage else "document_synthesis",
        model=selected_model,
    )
    message = response.choices[0].message
    return str(message.content or "").strip() or None


async def _safe_synthesize_from_document_evidence(
    *,
    history: List[Dict[str, Any]],
    user_context: UserContext,
    selected_model: str,
    mode: str,
    stage: str | None,
    reason: str,
) -> str | None:
    try:
        return await _synthesize_from_document_evidence(
            history=history,
            user_context=user_context,
            selected_model=selected_model,
            mode=mode,
            stage=stage,
            reason=reason,
        )
    except Exception as exc:
        log.warning(
            "agent.document_synthesis_failed",
            error=str(exc),
            reason=reason,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
        return None


def _log_agent_turn_start(
    *,
    turn: int,
    max_turns: int,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
    history: List[Dict[str, Any]],
) -> None:
    summary = _turn_state_summary(history)
    log.info(
        "agent.turn_start",
        turn=turn,
        max_turns=max_turns,
        mode=mode,
        stage=stage,
        model=selected_model,
        session_id=user_context.session_id,
        task_id=user_context.task_id,
        **summary,
    )


def _log_agent_tool_turn(
    *,
    turn: int,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
    tool_calls: List[Any],
    tool_results: List[Dict[str, Any]],
    repeated_signature_count: int,
) -> None:
    tool_names = [_tool_call_name(call) for call in tool_calls]
    result_fingerprints = _tool_result_fingerprints(tool_results)
    log_payload = {
        "turn": turn,
        "mode": mode,
        "stage": stage,
        "model": selected_model,
        "session_id": user_context.session_id,
        "task_id": user_context.task_id,
        "tools": tool_names,
        "result_fingerprints": result_fingerprints,
        "tool_signature": _tool_call_signature(tool_calls, tool_results),
        "repeated_signature_count": repeated_signature_count,
    }
    if repeated_signature_count >= 2:
        log.warning("agent.tool_turn_repeated", **log_payload)
    else:
        log.info("agent.tool_turn", **log_payload)


def _log_agent_final_turn(
    *,
    turn: int,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
    content: str,
) -> None:
    log.info(
        "agent.final_turn",
        turn=turn,
        mode=mode,
        stage=stage,
        model=selected_model,
        session_id=user_context.session_id,
        task_id=user_context.task_id,
        content_fingerprint=_content_fingerprint(content),
        content_chars=len(str(content or "")),
    )


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


def _failed_delete_message(tool_calls: List[Any], tool_results: List[Dict[str, Any]]) -> str | None:
    for call, result in zip(tool_calls, tool_results):
        if _tool_call_name(call) != "delete_event":
            continue
        content = str(result.get("content", "")).strip()
        lowered = content.lower()
        if any(lowered.startswith(prefix) for prefix in FAILED_DELETE_PREFIXES):
            return content
    return None


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


def _repeated_tool_signature_message(
    *,
    tool_calls: List[Any],
    repeated_count: int,
    history: List[Dict[str, Any]],
) -> str:
    tool_names = [name for name in (_tool_call_name(call) for call in tool_calls) if name]
    tool_label = ", ".join(tool_names[:4]) or "the same tools"
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I stopped after repeating the same tool cycle without enough progress. "
            f"Repeated tools: {tool_label}. "
            f"Recent results were: {' | '.join(snippets)}. "
            "Try narrowing the scope or giving me a more specific target."
        )
    return (
        "I stopped after repeating the same tool cycle without enough progress. "
        f"Repeated tools: {tool_label}. Try narrowing the scope or giving me a more specific target."
    )


def _repeated_semantic_research_message(
    *,
    tool_calls: List[Any],
    history: List[Dict[str, Any]],
) -> str:
    tool_names = [name for name in (_tool_call_name(call) for call in tool_calls) if name]
    tool_label = ", ".join(tool_names[:4]) or "the same research tools"
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I stopped because I was repeating the same research search with slightly different limits instead of converging on a summary. "
            f"Repeated tools: {tool_label}. "
            f"Recent results were: {' | '.join(snippets)}. "
            "Try narrowing the time window or topic, or ask me to summarize the strongest results already found."
        )
    return (
        "I stopped because I was repeating the same research search with slightly different limits instead of converging on a summary. "
        f"Repeated tools: {tool_label}. Try narrowing the time window or topic, or ask me to summarize the strongest results already found."
    )


def _repeated_document_retrieval_message(
    *,
    tool_calls: List[Any],
    history: List[Dict[str, Any]],
) -> str:
    tool_names = [name for name in (_tool_call_name(call) for call in tool_calls) if name]
    tool_label = ", ".join(tool_names[:4]) or "the same document tools"
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I stopped because I was repeating the same document lookup instead of converging on an answer. "
            f"Repeated tools: {tool_label}. "
            f"Recent results were: {' | '.join(snippets)}. "
            "Ask me to summarize what I already found or name the exact file you want."
        )
    return (
        "I stopped because I was repeating the same document lookup instead of converging on an answer. "
        f"Repeated tools: {tool_label}. Ask me to summarize what I already found or name the exact file you want."
    )


def _is_document_tool_signature(signature: str | None) -> bool:
    return bool(signature) and any(signature.startswith(f"{name}:") for name in DOCUMENT_RETRIEVAL_TOOL_NAMES)


def _is_exploration_tool_turn(tool_calls: List[Any]) -> bool:
    tool_names = [name for name in (_tool_call_name(call) for call in tool_calls) if name]
    return bool(tool_names) and all(name in EXPLORATION_TOOL_NAMES for name in tool_names)


def _exploration_churn_detected(signatures: List[str]) -> bool:
    if not signatures:
        return False
    window = max(2, int(settings.agent_exploration_churn_window))
    max_unique = max(1, int(settings.agent_exploration_churn_max_unique_signatures))
    if len(signatures) < window:
        return False
    recent = signatures[-window:]
    return len(set(recent)) <= max_unique


def _exploration_churn_message(
    *,
    tool_calls: List[Any],
    history: List[Dict[str, Any]],
) -> str:
    tool_names = [name for name in (_tool_call_name(call) for call in tool_calls) if name]
    tool_label = ", ".join(tool_names[:4]) or "repo exploration tools"
    snippets = _recent_tool_snippets(history)
    if snippets:
        return (
            "I stopped because the repo exploration was cycling through the same small set of results without enough new signal. "
            f"Recent exploration tools: {tool_label}. "
            f"Recent results were: {' | '.join(snippets)}. "
            "Try narrowing the roots, excluding more paths, or pointing me at a smaller target."
        )
    return (
        "I stopped because the repo exploration was cycling through the same small set of results without enough new signal. "
        f"Recent exploration tools: {tool_label}. Try narrowing the roots, excluding more paths, or pointing me at a smaller target."
    )


def _max_turns_message(history: List[Dict[str, Any]]) -> str:
    document_evidence = _recent_document_evidence(history, limit=1)
    if document_evidence:
        target = str(document_evidence[0].get("target") or "").strip()
        snippets = _recent_tool_snippets(history)
        if target and snippets:
            return (
                "I ran out of turns before I could finish that document workflow cleanly. "
                f"I was working on {target}. "
                f"Recent evidence was: {' | '.join(snippets)}. "
                "Ask me to summarize what I already found or narrow the exact section you want."
            )
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


def _recent_immediate_action_followup_hint(messages: List[Dict[str, Any]]) -> str | None:
    latest_user = _latest_user_message_text(messages).strip().lower()
    if latest_user not in {"yes", "yes.", "yes please", "please do", "go ahead", "go ahead.", "proceed", "proceed.", "do it", "do it.", "run it", "run it."}:
        return None

    previous_assistant = ""
    for message in reversed(messages[:-1]):
        if str(message.get("role") or "") == "assistant":
            previous_assistant = str(message.get("content") or "").strip().lower()
            break
    if not previous_assistant:
        return None

    immediate_markers = (
        "shall i run this now",
        "shall i proceed",
        "want me to proceed",
        "want me to run this now",
        "should i run this now",
        "i can run this now",
        "i can do this now",
    )
    action_markers = (
        "workspace",
        "file",
        "archive",
        "trim",
        "append",
        "write",
        "report",
        "directory",
    )
    if not any(marker in previous_assistant for marker in immediate_markers):
        return None
    if not any(marker in previous_assistant for marker in action_markers):
        return None

    return (
        "The user's latest short confirmation is approval to execute the concrete action proposed in the prior assistant message now in this chat. "
        "Prefer performing the action with available tools. Do not use propose_task_draft or create_task unless the user explicitly asks to save, schedule, automate, or create a task."
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


_IMAGE_PLACEMENT_INSTRUCTION = (
    "Final response presentation: include this generated image exactly once using Markdown "
    "`![concise alt text](IMAGE_PATH)`, replacing IMAGE_PATH with the exact image_path above. "
    "Place the reference immediately after the paragraph or section it illustrates instead of collecting images at the end."
)
_IMAGE_PLACEMENT_INSTRUCTION_MARKER = "Final response presentation:"


def _apply_generated_image_response_contract(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add presentation guidance to generated-image tool results used for synthesis."""
    tool_lookup = _tool_name_lookup(history)
    updated = list(history)
    changed = False

    for index, message in enumerate(history):
        if str(message.get("role") or "") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_lookup.get(tool_call_id) != "generate_image":
            continue
        content = str(message.get("content") or "")
        payload = _parse_tool_json_result(content)
        image_path = str((payload or {}).get("image_path") or (payload or {}).get("path") or "").strip()
        if not image_path or _IMAGE_PLACEMENT_INSTRUCTION_MARKER in content:
            continue
        augmented = dict(message)
        augmented["content"] = f"{content.rstrip()}\n\n{_IMAGE_PLACEMENT_INSTRUCTION.replace('IMAGE_PATH', image_path)}"
        updated[index] = augmented
        changed = True

    return updated if changed else history


def reset_task_handoff_payload() -> contextvars.Token:
    return _task_handoff_payload.set(None)


def restore_task_handoff_payload(token: contextvars.Token) -> None:
    _task_handoff_payload.reset(token)


def get_task_handoff_payload() -> dict[str, Any] | None:
    payload = _task_handoff_payload.get()
    return dict(payload) if isinstance(payload, dict) else None


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
        if tool_name == "propose_task_draft" and payload and payload.get("proposed") is True:
            _task_handoff_payload.set({"task_draft": payload})
            confirmation = str(payload.get("task_confirmation") or "").strip()
            if confirmation:
                return confirmation
            title = str(payload.get("title") or "").strip()
            schedule = str(payload.get("schedule") or "").strip()
            recipe_family = str(((payload.get("task_recipe") or {}).get("family") or "")).strip()
            suffix = f" Schedule: {schedule}." if schedule else ""
            if recipe_family:
                suffix = f" Draft family: {recipe_family}.{suffix}"
            if title:
                return f"Prepared task draft '{title}'.{suffix}"
            return f"Prepared a task draft.{suffix}"

    for tool_name, payload in paired:
        if tool_name == "update_task" and payload and payload.get("updated") is True:
            if "run now" in latest_user:
                continue
            confirmation = str(payload.get("task_confirmation") or "").strip()
            if confirmation:
                return confirmation
            title = str(payload.get("title") or "").strip()
            task_id = payload.get("task_id")
            schedule = str(payload.get("schedule") or "").strip()
            recipe_family = str(((payload.get("task_recipe") or {}).get("family") or "")).strip()
            suffix = f" Schedule: {schedule}." if schedule else ""
            if recipe_family:
                suffix = f" Recipe: {recipe_family}.{suffix}"
            if title:
                return f"Updated task '{title}' (task_id={task_id}).{suffix}"
            return f"Updated task {task_id}.{suffix}"

    saw_plan = any(
        tool_name in {"create_task_plan", "create_and_run_task_plan"} and payload is not None
        for tool_name, payload in paired
    )
    for tool_name, payload in paired:
        if tool_name == "create_task" and payload and payload.get("created") is True:
            confirmation = str(payload.get("task_confirmation") or "").strip()
            if confirmation:
                return confirmation
            task_type = str(payload.get("task_type") or "").strip().lower()
            title = str(payload.get("title") or "").strip()
            task_id = payload.get("task_id")
            schedule = str(payload.get("schedule") or "").strip()
            profile = str(payload.get("profile") or "").strip()
            recipe_family = str(((payload.get("task_recipe") or {}).get("family") or "")).strip()
            if saw_plan or task_type == "recurring" or profile in {"topic_watcher", "iss_pass_watcher", "rss_newspaper", "maintenance", "morning_briefing", "briefing"}:
                details = []
                if schedule:
                    details.append(f"schedule={schedule}")
                if profile:
                    details.append(f"profile={profile}")
                if recipe_family and recipe_family != profile:
                    details.append(f"recipe={recipe_family}")
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
    event_emitter: AgentEventEmitter | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream the final assistant text for a simple chat turn.

    Tools are disabled on this second pass so we do not reopen the tool-calling
    loop after the non-streaming probe determined the turn is a plain text
    response.
    """
    extra = _litellm_kwargs(selected_model, is_incognito=user_context.is_incognito)
    emitted = False

    try:
        stream_kwargs: Dict[str, Any] = {}
        if stream_usage_enabled():
            stream_kwargs["stream_options"] = {"include_usage": True}
        response = await _acompletion_with_budget(
            history=history,
            user_context=user_context,
            model=selected_model,
            mode="chat",
            stage=stage,
            stream=True,
            extra_kwargs=extra,
            stream_kwargs=stream_kwargs,
            event_emitter=event_emitter,
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


@dataclass
class _AgentLoopState:
    consecutive_failed_search_turns: int = 0
    previous_tool_signature: str = ""
    repeated_tool_signature_count: int = 0
    previous_semantic_tool_signature: str = ""
    repeated_semantic_tool_signature_count: int = 0
    recent_exploration_signatures: List[str] = field(default_factory=list)
    recent_rss_query_family_signatures: List[str] = field(default_factory=list)
    prior_recent_feed_fetches: int = 0
    empty_final_retries: int = 0


@dataclass
class _AgentLoopSetup:
    history: List[Dict[str, Any]]
    max_turns: int
    selected_model: str
    provider: ProviderCapabilities
    tools: List[Dict[str, Any]] | None
    extra: Dict[str, Any]
    state: _AgentLoopState
    rss_owned_headline_prompt: bool
    web_context_first: bool


@dataclass(frozen=True)
class _PreparedAgentTurn:
    number: int
    history: List[Dict[str, Any]]
    tools: List[Dict[str, Any]] | None
    tool_choice: Any


def _initialize_agent_loop(
    *,
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str,
    model_override: str | None,
    reasoning_effort_override: str | None,
    stage: str | None,
) -> _AgentLoopSetup:
    history = list(messages)
    max_turns = TURN_LIMITS.get(mode, 8)
    selected_model = model_override or settings.llm_model
    provider = resolve_provider_capabilities(
        selected_model,
        reasoning_effort_override=reasoning_effort_override,
    )
    tools = get_tools_for_user(user_context)
    if provider.tool_mode == "restricted":
        allowed = set(provider.allowed_tools)
        tools = [tool for tool in tools if tool["function"]["name"] in allowed]
    if provider.blocked_tools:
        blocked = set(provider.blocked_tools)
        tools = [tool for tool in tools if tool["function"]["name"] not in blocked]
    tools = _apply_local_tool_investigation_filters(
        tools=tools,
        model=selected_model,
        mode=mode,
        stage=stage,
        user_context=user_context,
        history=history,
    )
    rss_owned_headline_prompt = _is_rss_owned_headline_prompt(messages)
    if rss_owned_headline_prompt:
        log.info(
            "agent.headline_roundup_classified",
            rss_owned=True,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
    tools = _filter_tools_for_prompt(
        tools,
        messages=messages,
        rss_owned_headline_prompt=rss_owned_headline_prompt,
        mode=mode,
        stage=stage,
        selected_model=selected_model,
        user_context=user_context,
    )
    web_context_first = bool(
        _prompt_benefits_from_web_context(messages)
        and any(
            str(((tool.get("function") or {}).get("name") or "")).strip() == "web_context"
            for tool in tools
        )
    )
    return _AgentLoopSetup(
        history=history,
        max_turns=max_turns,
        selected_model=selected_model,
        provider=provider,
        tools=tools,
        extra=_litellm_kwargs(selected_model, is_incognito=user_context.is_incognito),
        state=_AgentLoopState(),
        rss_owned_headline_prompt=rss_owned_headline_prompt,
        web_context_first=web_context_first,
    )


async def _prepare_agent_turn(
    *,
    turn_number: int,
    setup: _AgentLoopSetup,
    user_context: UserContext,
    mode: str,
    stage: str | None,
    event_emitter: AgentEventEmitter | None,
) -> _PreparedAgentTurn:
    turn_history, turn_tools = _apply_local_tool_guardrail(
        history=setup.history,
        tools=setup.tools,
        model=setup.selected_model,
        mode=mode,
        stage=stage,
        user_context=user_context,
    )
    turn_tools, tool_choice = _apply_web_context_first_turn_policy(
        turn_tools,
        turn_number=turn_number,
        enabled=setup.web_context_first,
    )
    if tool_choice != "auto":
        log.info(
            "agent.web_context_first_turn",
            model=setup.selected_model,
            mode=mode,
            stage=stage,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
            depth=_web_context_depth_for_prompt(setup.history),
        )
    turn_history = _apply_generated_image_response_contract(turn_history)
    _log_agent_turn_start(
        turn=turn_number,
        max_turns=setup.max_turns,
        mode=mode,
        stage=stage,
        selected_model=setup.selected_model,
        user_context=user_context,
        history=setup.history,
    )
    if event_emitter is not None:
        if setup.history and setup.history[-1].get("role") == "tool":
            await event_emitter.emit(
                AgentEventType.SYNTHESIS_STARTED,
                turn=turn_number,
            )
        await event_emitter.emit(
            AgentEventType.MODEL_TURN_STARTED,
            turn=turn_number,
            model=setup.selected_model,
            mode=mode,
            stage=stage,
            history_length=len(turn_history),
            tools_enabled=bool(turn_tools),
            tool_count=len(turn_tools or []),
        )
    return _PreparedAgentTurn(
        number=turn_number,
        history=turn_history,
        tools=turn_tools,
        tool_choice=tool_choice,
    )


async def _execute_agent_tool_turn(
    *,
    original_messages: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    normalized_message: Dict[str, Any],
    state: _AgentLoopState,
    turn_number: int,
    max_turns: int,
    rss_owned_headline_prompt: bool,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
    event_emitter: AgentEventEmitter | None,
    pre_tool_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None,
    runtime_message_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None,
    streaming: bool,
) -> str | None:
    """Execute one tool turn and apply the shared convergence policy."""
    normalized_tool_calls = list(normalized_message.get("tool_calls") or [])
    normalized_tool_calls = _rewrite_web_context_tool_calls(
        normalized_tool_calls,
        messages=original_messages,
    )
    normalized_tool_calls, state.prior_recent_feed_fetches = _rewrite_headline_rss_tool_calls(
        normalized_tool_calls,
        rss_owned_headline_prompt=rss_owned_headline_prompt,
        prior_recent_feed_fetches=state.prior_recent_feed_fetches,
        mode=mode,
        stage=stage,
        selected_model=selected_model,
        user_context=user_context,
    )
    normalized_message["tool_calls"] = normalized_tool_calls
    history[-1] = normalized_message

    await emit_tool_requested_events(
        event_emitter,
        normalized_tool_calls,
        turn=turn_number,
    )
    if pre_tool_callback is not None:
        await pre_tool_callback(normalized_tool_calls)

    raw_tool_results = await dispatch_tool_calls(normalized_tool_calls, user_context)
    structured_tool_results = normalize_tool_call_results(normalized_tool_calls, raw_tool_results)
    tool_results = [result.to_message() for result in structured_tool_results]
    await emit_tool_completed_events(
        event_emitter,
        normalized_tool_calls,
        structured_tool_results,
        turn=turn_number,
    )
    history.extend(tool_results)
    runtime_messages = [normalized_message, *tool_results]
    _record_agent_runtime_messages(runtime_messages)
    if runtime_message_callback is not None:
        await runtime_message_callback(runtime_messages)

    current_tool_signature = _tool_call_signature(normalized_tool_calls, tool_results)
    current_semantic_tool_signature = _semantic_tool_signature(normalized_tool_calls)
    current_is_document_signature = _is_document_tool_signature(current_semantic_tool_signature)
    current_rss_query_family_signature = _rss_query_family_signature(normalized_tool_calls)

    if current_tool_signature == state.previous_tool_signature:
        state.repeated_tool_signature_count += 1
    else:
        state.repeated_tool_signature_count = 0
        state.previous_tool_signature = current_tool_signature
    if (
        current_semantic_tool_signature
        and current_semantic_tool_signature == state.previous_semantic_tool_signature
    ):
        state.repeated_semantic_tool_signature_count += 1
    else:
        state.repeated_semantic_tool_signature_count = 0
        state.previous_semantic_tool_signature = current_semantic_tool_signature or ""
    if _is_exploration_tool_turn(normalized_tool_calls):
        state.recent_exploration_signatures.append(current_tool_signature)
        state.recent_exploration_signatures = state.recent_exploration_signatures[
            -max(2, int(settings.agent_exploration_churn_window)) :
        ]
    else:
        state.recent_exploration_signatures = []
    if current_rss_query_family_signature:
        state.recent_rss_query_family_signatures.append(current_rss_query_family_signature)
        state.recent_rss_query_family_signatures = state.recent_rss_query_family_signatures[-6:]

    _log_agent_tool_turn(
        turn=turn_number,
        mode=mode,
        stage=stage,
        selected_model=selected_model,
        user_context=user_context,
        tool_calls=normalized_tool_calls,
        tool_results=tool_results,
        repeated_signature_count=state.repeated_tool_signature_count,
    )
    failed_delete = _failed_delete_message(normalized_tool_calls, tool_results)
    if failed_delete:
        return failed_delete
    task_handoff = _task_handoff_message(original_messages, normalized_tool_calls, tool_results)
    if task_handoff:
        return task_handoff

    metrics.inc_tool_calls(len(normalized_tool_calls))
    if _is_failed_search_turn(normalized_tool_calls, tool_results):
        state.consecutive_failed_search_turns += 1
        if state.consecutive_failed_search_turns >= REPEATED_FAILED_SEARCH_TURN_THRESHOLD:
            _record_loop_event(
                event_type="failed_search_loop",
                stage=stage,
                mode=mode,
                model=selected_model,
                details={"turn": turn_number, "threshold": REPEATED_FAILED_SEARCH_TURN_THRESHOLD},
            )
            log.info(
                "Stopping repeated failed search loop",
                turn=turn_number,
                model=selected_model,
                mode=mode,
                stage=stage,
            )
            return _repeated_failed_search_message(history)
    else:
        state.consecutive_failed_search_turns = 0

    if state.repeated_tool_signature_count >= int(settings.agent_repeated_tool_signature_threshold):
        _record_loop_event(
            event_type="repeated_tool_signature",
            stage=stage,
            mode=mode,
            model=selected_model,
            details={
                "turn": turn_number,
                "repeated_count": state.repeated_tool_signature_count,
                "tool_signature": current_tool_signature,
            },
        )
        return _repeated_tool_signature_message(
            tool_calls=normalized_tool_calls,
            repeated_count=state.repeated_tool_signature_count,
            history=history,
        )

    if (
        current_semantic_tool_signature
        and state.repeated_semantic_tool_signature_count
        >= int(settings.agent_repeated_semantic_tool_signature_threshold)
    ):
        _record_loop_event(
            event_type="repeated_semantic_tool_signature",
            stage=stage,
            mode=mode,
            model=selected_model,
            details={
                "turn": turn_number,
                "repeated_count": state.repeated_semantic_tool_signature_count,
                "tool_signature": current_semantic_tool_signature,
            },
        )
        if current_is_document_signature:
            synthesized = await _safe_synthesize_from_document_evidence(
                history=history,
                user_context=user_context,
                selected_model=selected_model,
                mode=mode,
                stage=stage,
                reason="document_query_family_churn",
            )
            if synthesized:
                return synthesized
            return _repeated_document_retrieval_message(
                tool_calls=normalized_tool_calls,
                history=history,
            )
        synthesized = await _safe_synthesize_from_rss_evidence(
            history=history,
            user_context=user_context,
            selected_model=selected_model,
            mode=mode,
            stage=stage,
            reason="repeated_semantic_tool_signature",
        )
        if synthesized:
            return synthesized
        return _repeated_semantic_research_message(
            tool_calls=normalized_tool_calls,
            history=history,
        )

    if current_rss_query_family_signature:
        family_count = state.recent_rss_query_family_signatures.count(current_rss_query_family_signature)
        if family_count >= int(settings.agent_repeated_rss_query_family_threshold):
            _record_loop_event(
                event_type="rss_query_family_churn",
                stage=stage,
                mode=mode,
                model=selected_model,
                details={
                    "turn": turn_number,
                    "family_signature": current_rss_query_family_signature,
                    "family_count": family_count,
                },
            )
            synthesized = await _safe_synthesize_from_rss_evidence(
                history=history,
                user_context=user_context,
                selected_model=selected_model,
                mode=mode,
                stage=stage,
                reason="rss_query_family_churn",
            )
            if synthesized:
                return synthesized
            return _repeated_semantic_research_message(
                tool_calls=normalized_tool_calls,
                history=history,
            )
        if (
            rss_owned_headline_prompt
            and len(state.recent_rss_query_family_signatures)
            >= int(settings.agent_headline_roundup_rss_turn_cap)
        ):
            _record_loop_event(
                event_type="headline_roundup_convergence",
                stage=stage,
                mode=mode,
                model=selected_model,
                details={
                    "turn": turn_number,
                    "family_signature": current_rss_query_family_signature,
                    "rss_turns": len(state.recent_rss_query_family_signatures),
                },
            )
            synthesized = await _safe_synthesize_from_rss_evidence(
                history=history,
                user_context=user_context,
                selected_model=selected_model,
                mode=mode,
                stage=stage,
                reason="headline_roundup_convergence",
            )
            if synthesized:
                return synthesized

    if current_is_document_signature and _has_nonempty_document_summary_evidence(history):
        if any(_tool_call_name(call) == "search_library" for call in normalized_tool_calls):
            _record_loop_event(
                event_type="document_summary_followed_by_search",
                stage=stage,
                mode=mode,
                model=selected_model,
                details={
                    "turn": turn_number,
                    "tool_names": [_tool_call_name(call) for call in normalized_tool_calls],
                },
            )
            synthesized = await _safe_synthesize_from_document_evidence(
                history=history,
                user_context=user_context,
                selected_model=selected_model,
                mode=mode,
                stage=stage,
                reason="document_summary_already_available",
            )
            if synthesized:
                return synthesized
    if current_is_document_signature and turn_number >= max_turns - 1:
        synthesized = await _safe_synthesize_from_document_evidence(
            history=history,
            user_context=user_context,
            selected_model=selected_model,
            mode=mode,
            stage=stage,
            reason="turn_budget_near_exhaustion",
        )
        if synthesized:
            return synthesized

    if _exploration_churn_detected(state.recent_exploration_signatures):
        _record_loop_event(
            event_type="exploration_churn",
            stage=stage,
            mode=mode,
            model=selected_model,
            details={
                "turn": turn_number,
                "recent_signatures": list(state.recent_exploration_signatures),
                "window": int(settings.agent_exploration_churn_window),
                "max_unique_signatures": int(settings.agent_exploration_churn_max_unique_signatures),
            },
        )
        return _exploration_churn_message(
            tool_calls=normalized_tool_calls,
            history=history,
        )

    log.info(
        "Tool calls executed (streaming turn)" if streaming else "Tool calls executed",
        turn=turn_number,
        tools=[_tool_call_name(call) for call in normalized_tool_calls],
        model=selected_model,
        mode=mode,
        stage=stage,
    )
    return None


async def _complete_agent_turn(
    *,
    history: List[Dict[str, Any]],
    turn_history: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    user_context: UserContext,
    selected_model: str,
    mode: str,
    stage: str | None,
    extra: Dict[str, Any],
    usage_stage: str | None,
    error_message: str,
    tool_choice: Any = "auto",
    event_emitter: AgentEventEmitter | None = None,
) -> Any:
    """Invoke one compatibility completion with shared local-tool recovery."""
    try:
        response = await _acompletion_with_budget(
            history=turn_history,
            user_context=user_context,
            model=selected_model,
            mode=mode,
            stage=stage,
            tools=tools,
            tool_choice=tool_choice,
            extra_kwargs=extra,
            event_emitter=event_emitter,
        )
    except Exception as exc:
        if tools and (
            _is_local_tool_json_parse_error(exc, selected_model)
            or _is_local_tool_unsupported_error(exc, selected_model)
        ):
            _log_local_tool_event(
                event=(
                    "LLM local_tool_unsupported_fallback"
                    if _is_local_tool_unsupported_error(exc, selected_model)
                    else "LLM local_tool_json_parse_fallback"
                ),
                history=history,
                tools=tools,
                model=selected_model,
                mode=mode,
                stage=stage,
                user_context=user_context,
                error=exc,
            )
            fallback_history = _build_tool_parse_fallback_history(history)
            response = await _acompletion_with_budget(
                history=fallback_history,
                user_context=user_context,
                model=selected_model,
                mode=mode,
                stage=f"{stage}_local_tool_fallback" if stage else "local_tool_fallback",
                extra_kwargs=extra,
                event_emitter=event_emitter,
            )
        else:
            log.error(
                error_message,
                error=str(exc),
                model=selected_model,
                mode=mode,
                stage=stage,
            )
            raise
    await record_llm_usage_event(response, stage=usage_stage, model=selected_model)
    return response


async def _run_agent_chunks(
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str = "chat",
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    stage: str | None = None,
    runtime_message_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    pre_tool_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    provisional_text_callback: Callable[[str, str], Awaitable[None]] | None = None,
    event_emitter: AgentEventEmitter | None = None,
    streaming: bool = False,
) -> AsyncGenerator[str, None]:
    """Run the canonical agent loop and yield final committed text chunks."""
    if streaming or mode == "chat":
        unsupported_api_message = _unsupported_alphavantage_request_message(messages)
        if unsupported_api_message:
            yield unsupported_api_message
            return

    setup = _initialize_agent_loop(
        messages=messages,
        user_context=user_context,
        mode=mode,
        model_override=model_override,
        reasoning_effort_override=reasoning_effort_override,
        stage=stage,
    )
    history = setup.history
    max_turns = setup.max_turns
    selected_model = setup.selected_model
    extra = setup.extra
    loop_state = setup.state
    rss_owned_headline_prompt = setup.rss_owned_headline_prompt
    native_streaming_enabled = streaming and setup.provider.native_streaming
    if streaming:
        log.info(
            "agent.native_streaming_policy",
            selected=native_streaming_enabled,
            model=selected_model,
            mode=mode,
            stage=stage,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )

    for turn in range(max_turns):
        turn_number = turn + 1
        prepared_turn = await _prepare_agent_turn(
            turn_number=turn_number,
            setup=setup,
            user_context=user_context,
            mode=mode,
            stage=stage,
            event_emitter=event_emitter,
        )
        turn_history = prepared_turn.history
        turn_tools = prepared_turn.tools
        turn_tool_choice = prepared_turn.tool_choice
        native_turn: ModelTurnResult | None = None
        native_text_emitted = False
        provisional_text_active = False
        reasoning_event_emitted = False
        if native_streaming_enabled:
            accumulator = ModelTurnAccumulator()
            provider_stream = None
            event_counts: dict[str, int] = {}
            stream_started = time.perf_counter()
            first_event_ms: float | None = None
            native_extra = dict(extra)
            reasoning_effort = setup.provider.native_reasoning_effort
            if reasoning_effort:
                native_extra["reasoning_effort"] = reasoning_effort
            stream_kwargs: dict[str, Any] = {}
            if stream_usage_enabled():
                stream_kwargs["stream_options"] = {"include_usage": True}
            try:
                provider_stream = await _acompletion_with_budget(
                    history=turn_history,
                    user_context=user_context,
                    model=selected_model,
                    mode=mode,
                    stage=stage,
                    stream=True,
                    tools=turn_tools,
                    tool_choice=turn_tool_choice,
                    extra_kwargs=native_extra,
                    stream_kwargs=stream_kwargs,
                    event_emitter=event_emitter,
                )
                try:
                    async for event in iter_model_stream_events(provider_stream):
                        if first_event_ms is None:
                            first_event_ms = (time.perf_counter() - stream_started) * 1000.0
                        event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
                        accumulator.add(event)
                        if (
                            event.kind == "reasoning_delta"
                            and event.text
                            and not reasoning_event_emitted
                            and event_emitter is not None
                        ):
                            reasoning_event_emitted = True
                            await event_emitter.emit(
                                AgentEventType.REASONING_STARTED,
                                turn=turn_number,
                            )
                        if event.kind == "text_delta" and event.text:
                            if turn_tools and provisional_text_callback is not None:
                                provisional_text_active = True
                                await provisional_text_callback("delta", event.text)
                            elif not turn_tools:
                                native_text_emitted = True
                                yield event.text
                finally:
                    try:
                        await close_provider_stream(provider_stream)
                    finally:
                        _write_reasoning_tap(
                            "".join(accumulator.reasoning_parts),
                            user_context=user_context,
                            model=selected_model,
                            stage=stage,
                            turn=turn_number,
                        )
                native_turn = accumulator.finish()
                if native_turn.usage:
                    await record_llm_usage_event(
                        {"usage": native_turn.usage},
                        stage=f"{stage}_native_stream" if stage else "native_stream",
                        model=selected_model,
                    )
                log.info(
                    "agent.native_streaming_turn_finished",
                    model=selected_model,
                    mode=mode,
                    stage=stage,
                    session_id=user_context.session_id,
                    task_id=user_context.task_id,
                    turn=turn_number,
                    first_event_ms=round(first_event_ms or 0.0, 2),
                    elapsed_ms=round((time.perf_counter() - stream_started) * 1000.0, 2),
                    event_counts=event_counts,
                    finish_reason=native_turn.finish_reason,
                    tool_call_count=len(native_turn.tool_calls),
                    duplicate_final_pass_avoided=True,
                )
            except Exception as e:
                if provisional_text_active and provisional_text_callback is not None:
                    await provisional_text_callback("reset", "")
                    provisional_text_active = False
                if accumulator.event_count > 0:
                    log.error(
                        "agent.native_streaming_partial_failure",
                        error_type=type(e).__name__,
                        model=selected_model,
                        mode=mode,
                        stage=stage,
                        session_id=user_context.session_id,
                        task_id=user_context.task_id,
                        turn=turn_number,
                        event_count=accumulator.event_count,
                    )
                    raise ModelStreamError(
                        "Native model stream failed after partial progress"
                    ) from None
                log.warning(
                    "agent.native_streaming_fallback",
                    error_type=type(e).__name__,
                    model=selected_model,
                    mode=mode,
                    stage=stage,
                    session_id=user_context.session_id,
                    task_id=user_context.task_id,
                    turn=turn_number,
                    failure_phase="before_first_event",
                )

        if native_turn is None:
            # Compatibility path: non-streaming probe followed by optional final stream.
            response = await _complete_agent_turn(
                history=history,
                turn_history=turn_history,
                tools=turn_tools,
                user_context=user_context,
                selected_model=selected_model,
                mode=mode,
                stage=stage,
                extra=extra,
                usage_stage=(f"{stage}_probe" if stage else "stream_probe") if streaming else stage,
                error_message="LLM call failed (streaming turn)" if streaming else "LLM call failed",
                tool_choice=turn_tool_choice,
                event_emitter=event_emitter,
            )
            message: Any = response.choices[0].message
        else:
            message = native_turn

        if message.tool_calls:
            if provisional_text_active and provisional_text_callback is not None:
                await provisional_text_callback("reset", "")
                provisional_text_active = False
            normalized_message = (
                message.assistant_message()
                if isinstance(message, ModelTurnResult)
                else _normalize_tool_calls(message.model_dump(exclude_none=True))
            )
            # Streamed narration preceding a tool call is provisional working state.
            if streaming:
                normalized_message["content"] = ""
            if native_turn is not None:
                _drop_stale_reasoning_content(history)
            history.append(normalized_message)
            stop_response = await _execute_agent_tool_turn(
                original_messages=messages,
                history=history,
                normalized_message=normalized_message,
                state=loop_state,
                turn_number=turn_number,
                max_turns=max_turns,
                rss_owned_headline_prompt=rss_owned_headline_prompt,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                event_emitter=event_emitter,
                pre_tool_callback=pre_tool_callback,
                runtime_message_callback=runtime_message_callback,
                streaming=streaming,
            )
            if stop_response is not None:
                yield stop_response
                return
        else:
            final_content = str(message.content or "").strip()
            if not final_content:
                if loop_state.empty_final_retries < 1:
                    loop_state.empty_final_retries += 1
                    log.warning(
                        "agent.empty_final_answer_retry",
                        turn=turn_number,
                        model=selected_model,
                        mode=mode,
                        stage=stage,
                        session_id=user_context.session_id,
                        task_id=user_context.task_id,
                    )
                    if event_emitter is not None:
                        await event_emitter.emit(
                            AgentEventType.VALIDATION_RETRY,
                            turn=turn_number,
                            retry_reason="empty_final_answer",
                        )
                    history.append(
                        {
                            "role": "system",
                            "content": (
                                "The previous model turn returned no user-visible answer. "
                                "Respond now with a complete, concise answer to the user's latest request."
                            ),
                        }
                    )
                    continue
                fallback = (
                    "I couldn't produce a usable answer from the model response. "
                    "Please retry, or narrow the request if it continues."
                )
                log.error(
                    "agent.empty_final_answer_exhausted",
                    turn=turn_number,
                    model=selected_model,
                    mode=mode,
                    stage=stage,
                    session_id=user_context.session_id,
                    task_id=user_context.task_id,
                )
                yield fallback
                return
            _log_agent_final_turn(
                turn=turn_number,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                content=final_content,
            )
            if native_turn is not None:
                if provisional_text_active and provisional_text_callback is not None:
                    await provisional_text_callback("commit", "")
                if not native_text_emitted:
                    for token in _chunk_plain_text(final_content):
                        yield token
                return
            if not streaming:
                yield final_content
                return
            if setup.provider.skip_duplicate_final_stream:
                for token in _chunk_plain_text(final_content):
                    yield token
                return
            async for token in _stream_final_response(
                history,
                user_context,
                selected_model=selected_model,
                stage=stage,
                event_emitter=event_emitter,
            ):
                yield token
            return

    if not streaming:
        log.warning("Agent hit max turns without a final response", max_turns=max_turns)
    yield _max_turns_message(history)


async def run_agent(
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str = "chat",
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    stage: str | None = None,
    runtime_message_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    pre_tool_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    event_emitter: AgentEventEmitter | None = None,
) -> str:
    """Run the non-streaming loop and emit one ordered lifecycle stream."""
    selected_model = model_override or settings.llm_model
    emitter = event_emitter or AgentEventEmitter(
        session_id=user_context.session_id,
        task_id=user_context.task_id,
    )
    await emitter.start_once(
        mode=mode,
        model=selected_model,
        stage=stage,
        streaming=False,
    )
    try:
        result = "".join(
            [
                chunk
                async for chunk in _run_agent_chunks(
                    messages,
                    user_context,
                    mode=mode,
                    model_override=model_override,
                    reasoning_effort_override=reasoning_effort_override,
                    stage=stage,
                    runtime_message_callback=runtime_message_callback,
                    pre_tool_callback=pre_tool_callback,
                    event_emitter=emitter,
                    streaming=False,
                )
            ]
        )
    except asyncio.CancelledError:
        await emitter.terminal_once(AgentEventType.RUN_CANCELLED)
        raise
    except ApprovalRequired as exc:
        await emitter.emit(
            AgentEventType.APPROVAL_REQUIRED,
            tool_name=exc.tool_name,
            approval_kind=exc.approval_kind,
            reason=exc.reason,
        )
        raise
    except Exception as exc:
        await emitter.terminal_once(
            AgentEventType.RUN_FAILED,
            error_type=type(exc).__name__,
            failure_phase="agent_loop",
        )
        raise
    await emitter.terminal_once(
        AgentEventType.RUN_COMPLETED,
        content_chars=len(result),
    )
    return result


async def stream_agent(
    messages: List[Dict[str, Any]],
    user_context: UserContext,
    mode: str = "chat",
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    stage: str | None = None,
    runtime_message_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    pre_tool_callback: Callable[[List[Dict[str, Any]]], Awaitable[None]] | None = None,
    provisional_text_callback: Callable[[str, str], Awaitable[None]] | None = None,
    event_emitter: AgentEventEmitter | None = None,
) -> AsyncGenerator[str, None]:
    """Run the streaming loop and emit one ordered lifecycle stream."""
    selected_model = model_override or settings.llm_model
    emitter = event_emitter or AgentEventEmitter(
        session_id=user_context.session_id,
        task_id=user_context.task_id,
    )
    await emitter.start_once(
        mode=mode,
        model=selected_model,
        stage=stage,
        streaming=True,
    )
    wrapped_provisional_callback = wrap_provisional_text_callback(
        emitter,
        provisional_text_callback,
    )
    content_chars = 0
    inner_stream = _run_agent_chunks(
        messages,
        user_context,
        mode=mode,
        model_override=model_override,
        reasoning_effort_override=reasoning_effort_override,
        stage=stage,
        runtime_message_callback=runtime_message_callback,
        pre_tool_callback=pre_tool_callback,
        provisional_text_callback=wrapped_provisional_callback,
        event_emitter=emitter,
        streaming=True,
    )
    try:
        async for chunk in inner_stream:
            content_chars += len(chunk)
            if emitter.is_observed:
                await emitter.emit(
                    AgentEventType.TEXT_DELTA,
                    content=chunk,
                    provisional=False,
                )
            yield chunk
    except asyncio.CancelledError:
        await emitter.terminal_once(AgentEventType.RUN_CANCELLED)
        raise
    except ApprovalRequired as exc:
        await emitter.emit(
            AgentEventType.APPROVAL_REQUIRED,
            tool_name=exc.tool_name,
            approval_kind=exc.approval_kind,
            reason=exc.reason,
        )
        raise
    except GeneratorExit:
        await emitter.terminal_once(
            AgentEventType.RUN_CANCELLED,
            reason="consumer_closed_stream",
        )
        raise
    except Exception as exc:
        await emitter.terminal_once(
            AgentEventType.RUN_FAILED,
            error_type=type(exc).__name__,
            failure_phase="agent_loop",
        )
        raise
    finally:
        await inner_stream.aclose()
    await emitter.terminal_once(
        AgentEventType.RUN_COMPLETED,
        content_chars=content_chars,
    )
