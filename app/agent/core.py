"""
FruitcakeAI v5 — Agent core loop
LiteLLM-powered tool-calling loop. The LLM drives all orchestration —
it decides when to call tools and how to synthesize results.
"""

from __future__ import annotations

import hashlib
import json
import re
import contextvars
from typing import Any, AsyncGenerator, Dict, List

import litellm
import structlog

from app.agent.context import UserContext
from app.agent.tools import dispatch_tool_calls, get_tools_for_user
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

# Phase 4: task sessions get more turns for multi-step autonomous work
TURN_LIMITS: Dict[str, int] = {
    "chat": 8,
    "task": 16,
    "chat_orchestrated": 15,
}

REPEATED_FAILED_SEARCH_TURN_THRESHOLD = 5
ESTIMATED_CHARS_PER_TOKEN = 4
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
) -> List[Dict[str, Any]]:
    """Prepend the system prompt to the conversation history."""
    messages = [{"role": "system", "content": user_context.to_system_prompt()}]
    followup_hint = _recent_task_followup_hint(history)
    if followup_hint:
        messages.append({"role": "system", "content": followup_hint})
    return messages + history


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
    tool_choice: str | None = None,
    extra_kwargs: Dict[str, Any] | None = None,
    stream_kwargs: Dict[str, Any] | None = None,
) -> Any:
    extra_kwargs = dict(extra_kwargs or {})
    stream_kwargs = dict(stream_kwargs or {})

    projected_history, report = _project_history_for_model(history, aggressive=False)
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
    _record_budget_event(report, stage=stage, mode=mode, model=model)

    try:
        return await litellm.acompletion(
            model=model,
            messages=_build_messages(projected_history, user_context),
            stream=stream,
            tools=tools or None,
            tool_choice=tool_choice if tools else None,
            **extra_kwargs,
            **stream_kwargs,
        )
    except Exception as exc:
        if not settings.agent_overflow_retry_enabled or not _is_context_window_error(exc):
            raise
        aggressive_history, aggressive_report = _project_history_for_model(history, aggressive=True)
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
        _record_budget_event(aggressive_report, stage=stage, mode=mode, model=model)
        try:
            response = await litellm.acompletion(
                model=model,
                messages=_build_messages(aggressive_history, user_context),
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


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "").strip()
    return str(getattr(call, "id", "") or "").strip()


def _content_fingerprint(value: str, *, length: int = 12) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return "0" * max(1, length)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[: max(1, length)]


def _estimate_tokens(text: str) -> int:
    chars = len(str(text or ""))
    return max(1, chars // ESTIMATED_CHARS_PER_TOKEN) if chars else 0


def _estimate_message_tokens(message: Dict[str, Any]) -> int:
    total = _estimate_tokens(str(message.get("content") or ""))
    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        total += _estimate_tokens(_tool_call_name(call))
        total += _estimate_tokens(str(_tool_call_arguments(call)))
    return total


def _estimate_history_tokens(history: List[Dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(message) for message in history)


def _compact_text(value: str, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


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
    compact_summary = _compact_text(content, max_chars=max_chars)
    tool_call_id = str(message.get("tool_call_id") or "").strip()
    tool_name = tool_name_lookup.get(tool_call_id) or "unknown_tool"
    compacted = (
        "Compacted tool result.\n"
        f"Tool: {tool_name}\n"
        f"Tool call id: {tool_call_id or 'unknown'}\n"
        f"Fingerprint: {_content_fingerprint(content)}\n"
        f"Original chars: {len(content)}\n"
        f"Summary: {compact_summary}"
    )
    return {**message, "content": compacted}


def _compact_message_summary(
    message: Dict[str, Any],
    *,
    tool_name_lookup: dict[str, str],
) -> str:
    role = str(message.get("role") or "").strip() or "unknown"
    if role == "tool":
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        tool_name = tool_name_lookup.get(tool_call_id) or "unknown_tool"
        content = _compact_text(str(message.get("content") or ""), max_chars=140)
        return f"Tool {tool_name}: {content}"
    if message.get("tool_calls"):
        names = []
        for call in message.get("tool_calls") or []:
            names.append(_tool_call_name(call) or "unknown_tool")
        return f"Assistant requested tools: {', '.join(names[:5])}"
    content = _compact_text(str(message.get("content") or ""), max_chars=160)
    return f"{role.capitalize()}: {content}"


def _build_compaction_boundary_message(
    prefix: List[Dict[str, Any]],
    *,
    tool_name_lookup: dict[str, str],
) -> Dict[str, Any]:
    lines = [
        "Earlier conversation was compacted to stay within the context budget.",
        "Preserve these compacted facts unless later context contradicts them:",
    ]
    summaries: list[str] = []
    for message in prefix:
        summary = _compact_message_summary(message, tool_name_lookup=tool_name_lookup)
        if summary:
            summaries.append(f"- {summary}")
        if len(summaries) >= 10:
            break
    if not summaries:
        summaries.append("- Earlier tool and assistant turns were compacted.")
    lines.extend(summaries)
    return {"role": "system", "content": "\n".join(lines)}


def _project_history_for_model(
    history: List[Dict[str, Any]],
    *,
    aggressive: bool = False,
) -> tuple[List[Dict[str, Any]], dict[str, Any]]:
    projected = list(history)
    report: dict[str, Any] = {
        "aggressive": aggressive,
        "estimated_tokens_before": _estimate_history_tokens(history),
        "estimated_tokens_after": 0,
        "tool_results_compacted": 0,
        "compaction_boundary_applied": False,
        "boundary_messages_collapsed": 0,
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
        should_compact = aggressive or len(content) > int(settings.agent_tool_result_max_chars)
        if not should_compact and index not in recent_tool_indices:
            should_compact = len(content) > max(400, int(settings.agent_tool_result_max_chars) // 2)
        if not should_compact:
            continue
        projected[index] = _compact_tool_message(
            message,
            tool_name_lookup=tool_lookup,
            max_chars=int(settings.agent_tool_result_max_chars),
        )
        report["tool_results_compacted"] += 1

    estimated_after = _estimate_history_tokens(projected)
    keep_recent_messages = max(1, int(settings.agent_recent_messages_keep))
    if projected and (aggressive or estimated_after > int(settings.agent_history_soft_token_limit)):
        prefix = projected[:-keep_recent_messages]
        suffix = projected[-keep_recent_messages:]
        if prefix:
            projected = [_build_compaction_boundary_message(prefix, tool_name_lookup=tool_lookup)] + suffix
            report["compaction_boundary_applied"] = True
            report["boundary_messages_collapsed"] = len(prefix)
            estimated_after = _estimate_history_tokens(projected)

    report["estimated_tokens_after"] = estimated_after
    return projected, report


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
    if tool_name not in RSS_SEARCH_TOOL_NAMES:
        return None
    arguments = _tool_call_arguments(tool_calls[0])
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
    if len(tool_calls) != 1:
        return None
    tool_name = _tool_call_name(tool_calls[0])
    if tool_name not in RSS_RETRIEVAL_TOOL_NAMES:
        return None
    arguments = _tool_call_arguments(tool_calls[0])
    if tool_name == "search_my_feeds_timeline":
        query_family = _normalize_query_family(str(arguments.get("query") or ""))
        start = str(arguments.get("start_date") or "")
        end = str(arguments.get("end_date") or "")
        return f"{tool_name}:{query_family}:{start}:{end}"
    if tool_name == "search_my_feeds":
        query_family = _normalize_query_family(str(arguments.get("query") or ""))
        category = str(arguments.get("category") or "")
        return f"{tool_name}:{query_family}:{category}"
    sources = arguments.get("sources") or {}
    window = arguments.get("window") or {}
    source_mode = str((sources.get("mode") or "all")).strip().lower()
    window_mode = str((window.get("mode") or "all")).strip().lower()
    return f"{tool_name}:{source_mode}:{window_mode}"


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


def _filter_tools_for_prompt(
    tools: List[Dict[str, Any]],
    *,
    rss_owned_headline_prompt: bool,
    mode: str,
    stage: str | None,
    selected_model: str,
    user_context: UserContext,
) -> List[Dict[str, Any]]:
    if not rss_owned_headline_prompt or mode not in {"chat", "chat_orchestrated"}:
        return tools
    filtered = [tool for tool in tools if str(((tool.get("function") or {}).get("name") or "")).strip() != "web_search"]
    if len(filtered) != len(tools):
        log.info(
            "agent.headline_roundup_rss_lane",
            skipped_web_search=True,
            mode=mode,
            stage=stage,
            model=selected_model,
            session_id=user_context.session_id,
            task_id=user_context.task_id,
        )
    return filtered


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
    extra = _litellm_kwargs(selected_model)
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
        response = await _acompletion_with_budget(
            history=history,
            user_context=user_context,
            model=selected_model,
            mode="chat",
            stage=stage,
            stream=True,
            extra_kwargs=extra,
            stream_kwargs=stream_kwargs,
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
    previous_tool_signature = ""
    repeated_tool_signature_count = 0
    previous_semantic_tool_signature = ""
    repeated_semantic_tool_signature_count = 0
    recent_exploration_signatures: List[str] = []
    recent_rss_query_family_signatures: List[str] = []
    headline_roundup_prompt = _is_headline_roundup_prompt(messages)
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
        rss_owned_headline_prompt=rss_owned_headline_prompt,
        mode=mode,
        stage=stage,
        selected_model=selected_model,
        user_context=user_context,
    )
    prior_recent_feed_fetches = 0

    for turn in range(max_turns):
        turn_number = turn + 1
        _log_agent_turn_start(
            turn=turn_number,
            max_turns=max_turns,
            mode=mode,
            stage=stage,
            selected_model=selected_model,
            user_context=user_context,
            history=history,
        )
        try:
            response = await _acompletion_with_budget(
                history=history,
                user_context=user_context,
                model=selected_model,
                mode=mode,
                stage=stage,
                tools=tools,
                tool_choice="auto",
                extra_kwargs=extra,
            )
        except Exception as e:
            log.error("LLM call failed", error=str(e), model=selected_model, mode=mode, stage=stage)
            raise
        await record_llm_usage_event(response, stage=stage, model=selected_model)

        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # Append assistant turn to history (normalize tool_call args to str)
        normalized_message = _normalize_tool_calls(message.model_dump(exclude_none=True))
        history.append(normalized_message)

        # Check tool_calls directly — some Ollama models return finish_reason="stop"
        # even when tool calls are present, so we can't rely on finish_reason alone.
        if message.tool_calls:
            normalized_tool_calls = list(normalized_message.get("tool_calls") or [])
            normalized_tool_calls, prior_recent_feed_fetches = _rewrite_headline_rss_tool_calls(
                normalized_tool_calls,
                rss_owned_headline_prompt=rss_owned_headline_prompt,
                prior_recent_feed_fetches=prior_recent_feed_fetches,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
            )
            normalized_message["tool_calls"] = normalized_tool_calls
            history[-1] = normalized_message
            # Execute all tool calls, append results, then loop
            tool_results = await dispatch_tool_calls(normalized_tool_calls, user_context)
            history.extend(tool_results)
            _record_agent_runtime_messages([normalized_message, *tool_results])
            current_tool_signature = _tool_call_signature(normalized_tool_calls, tool_results)
            current_semantic_tool_signature = _semantic_tool_signature(normalized_tool_calls)
            current_rss_query_family_signature = _rss_query_family_signature(normalized_tool_calls)
            if current_tool_signature == previous_tool_signature:
                repeated_tool_signature_count += 1
            else:
                repeated_tool_signature_count = 0
                previous_tool_signature = current_tool_signature
            if current_semantic_tool_signature and current_semantic_tool_signature == previous_semantic_tool_signature:
                repeated_semantic_tool_signature_count += 1
            else:
                repeated_semantic_tool_signature_count = 0
                previous_semantic_tool_signature = current_semantic_tool_signature or ""
            if _is_exploration_tool_turn(message.tool_calls):
                recent_exploration_signatures.append(current_tool_signature)
                recent_exploration_signatures = recent_exploration_signatures[
                    -max(2, int(settings.agent_exploration_churn_window)) :
                ]
            else:
                recent_exploration_signatures = []
            if current_rss_query_family_signature:
                recent_rss_query_family_signatures.append(current_rss_query_family_signature)
                recent_rss_query_family_signatures = recent_rss_query_family_signatures[-6:]
            _log_agent_tool_turn(
                turn=turn_number,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                tool_calls=normalized_tool_calls,
                tool_results=tool_results,
                repeated_signature_count=repeated_tool_signature_count,
            )
            failed_delete = _failed_delete_message(normalized_tool_calls, tool_results)
            if failed_delete:
                return failed_delete
            task_handoff = _task_handoff_message(messages, normalized_tool_calls, tool_results)
            if task_handoff:
                return task_handoff
            metrics.inc_tool_calls(len(normalized_tool_calls))
            if _is_failed_search_turn(normalized_tool_calls, tool_results):
                consecutive_failed_search_turns += 1
                if consecutive_failed_search_turns >= REPEATED_FAILED_SEARCH_TURN_THRESHOLD:
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
                consecutive_failed_search_turns = 0
            if repeated_tool_signature_count >= int(settings.agent_repeated_tool_signature_threshold):
                _record_loop_event(
                    event_type="repeated_tool_signature",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "repeated_count": repeated_tool_signature_count,
                        "tool_signature": current_tool_signature,
                    },
                )
                return _repeated_tool_signature_message(
                    tool_calls=normalized_tool_calls,
                    repeated_count=repeated_tool_signature_count,
                    history=history,
                )
            if (
                current_semantic_tool_signature
                and repeated_semantic_tool_signature_count >= int(settings.agent_repeated_semantic_tool_signature_threshold)
            ):
                _record_loop_event(
                    event_type="repeated_semantic_tool_signature",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "repeated_count": repeated_semantic_tool_signature_count,
                        "tool_signature": current_semantic_tool_signature,
                    },
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
                family_count = recent_rss_query_family_signatures.count(current_rss_query_family_signature)
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
                    and len(recent_rss_query_family_signatures) >= int(settings.agent_headline_roundup_rss_turn_cap)
                ):
                    _record_loop_event(
                        event_type="headline_roundup_convergence",
                        stage=stage,
                        mode=mode,
                        model=selected_model,
                        details={
                            "turn": turn_number,
                            "family_signature": current_rss_query_family_signature,
                            "rss_turns": len(recent_rss_query_family_signatures),
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
            if _exploration_churn_detected(recent_exploration_signatures):
                _record_loop_event(
                    event_type="exploration_churn",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "recent_signatures": list(recent_exploration_signatures),
                        "window": int(settings.agent_exploration_churn_window),
                        "max_unique_signatures": int(settings.agent_exploration_churn_max_unique_signatures),
                    },
                )
                return _exploration_churn_message(
                    tool_calls=normalized_tool_calls,
                    history=history,
                )
            log.info(
                "Tool calls executed",
                turn=turn + 1,
                tools=[_tool_call_name(tc) for tc in normalized_tool_calls],
                model=selected_model,
                mode=mode,
                stage=stage,
            )
        else:
            # Final text response
            _log_agent_final_turn(
                turn=turn_number,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                content=message.content or "",
            )
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
    previous_tool_signature = ""
    repeated_tool_signature_count = 0
    previous_semantic_tool_signature = ""
    repeated_semantic_tool_signature_count = 0
    recent_exploration_signatures: List[str] = []
    recent_rss_query_family_signatures: List[str] = []
    headline_roundup_prompt = _is_headline_roundup_prompt(messages)
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
        rss_owned_headline_prompt=rss_owned_headline_prompt,
        mode=mode,
        stage=stage,
        selected_model=selected_model,
        user_context=user_context,
    )
    prior_recent_feed_fetches = 0

    for turn in range(max_turns):
        turn_number = turn + 1
        _log_agent_turn_start(
            turn=turn_number,
            max_turns=max_turns,
            mode=mode,
            stage=stage,
            selected_model=selected_model,
            user_context=user_context,
            history=history,
        )
        # Probe turn non-streaming so intermediate tool turns stay internal.
        try:
            response = await _acompletion_with_budget(
                history=history,
                user_context=user_context,
                model=selected_model,
                mode=mode,
                stage=stage,
                stream=False,
                tools=tools,
                tool_choice="auto",
                extra_kwargs=extra,
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
            normalized_message = _normalize_tool_calls(message.model_dump(exclude_none=True))
            history.append(normalized_message)
            normalized_tool_calls = list(normalized_message.get("tool_calls") or [])
            normalized_tool_calls, prior_recent_feed_fetches = _rewrite_headline_rss_tool_calls(
                normalized_tool_calls,
                rss_owned_headline_prompt=rss_owned_headline_prompt,
                prior_recent_feed_fetches=prior_recent_feed_fetches,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
            )
            normalized_message["tool_calls"] = normalized_tool_calls
            history[-1] = normalized_message
            tool_results = await dispatch_tool_calls(normalized_tool_calls, user_context)
            history.extend(tool_results)
            _record_agent_runtime_messages([normalized_message, *tool_results])
            current_tool_signature = _tool_call_signature(normalized_tool_calls, tool_results)
            current_semantic_tool_signature = _semantic_tool_signature(normalized_tool_calls)
            current_rss_query_family_signature = _rss_query_family_signature(normalized_tool_calls)
            if current_tool_signature == previous_tool_signature:
                repeated_tool_signature_count += 1
            else:
                repeated_tool_signature_count = 0
                previous_tool_signature = current_tool_signature
            if current_semantic_tool_signature and current_semantic_tool_signature == previous_semantic_tool_signature:
                repeated_semantic_tool_signature_count += 1
            else:
                repeated_semantic_tool_signature_count = 0
                previous_semantic_tool_signature = current_semantic_tool_signature or ""
            if _is_exploration_tool_turn(normalized_tool_calls):
                recent_exploration_signatures.append(current_tool_signature)
                recent_exploration_signatures = recent_exploration_signatures[
                    -max(2, int(settings.agent_exploration_churn_window)) :
                ]
            else:
                recent_exploration_signatures = []
            if current_rss_query_family_signature:
                recent_rss_query_family_signatures.append(current_rss_query_family_signature)
                recent_rss_query_family_signatures = recent_rss_query_family_signatures[-6:]
            _log_agent_tool_turn(
                turn=turn_number,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                tool_calls=normalized_tool_calls,
                tool_results=tool_results,
                repeated_signature_count=repeated_tool_signature_count,
            )
            failed_delete = _failed_delete_message(normalized_tool_calls, tool_results)
            if failed_delete:
                yield failed_delete
                return
            task_handoff = _task_handoff_message(messages, normalized_tool_calls, tool_results)
            if task_handoff:
                yield task_handoff
                return
            metrics.inc_tool_calls(len(normalized_tool_calls))
            if _is_failed_search_turn(normalized_tool_calls, tool_results):
                consecutive_failed_search_turns += 1
                if consecutive_failed_search_turns >= REPEATED_FAILED_SEARCH_TURN_THRESHOLD:
                    _record_loop_event(
                        event_type="failed_search_loop",
                        stage=stage,
                        mode=mode,
                        model=selected_model,
                        details={"turn": turn_number, "threshold": REPEATED_FAILED_SEARCH_TURN_THRESHOLD},
                    )
                    yield _repeated_failed_search_message(history)
                    return
            else:
                consecutive_failed_search_turns = 0
            if repeated_tool_signature_count >= int(settings.agent_repeated_tool_signature_threshold):
                _record_loop_event(
                    event_type="repeated_tool_signature",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "repeated_count": repeated_tool_signature_count,
                        "tool_signature": current_tool_signature,
                    },
                )
                yield _repeated_tool_signature_message(
                    tool_calls=normalized_tool_calls,
                    repeated_count=repeated_tool_signature_count,
                    history=history,
                )
                return
            if (
                current_semantic_tool_signature
                and repeated_semantic_tool_signature_count >= int(settings.agent_repeated_semantic_tool_signature_threshold)
            ):
                _record_loop_event(
                    event_type="repeated_semantic_tool_signature",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "repeated_count": repeated_semantic_tool_signature_count,
                        "tool_signature": current_semantic_tool_signature,
                    },
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
                    yield synthesized
                    return
                yield _repeated_semantic_research_message(
                    tool_calls=normalized_tool_calls,
                    history=history,
                )
                return
            if current_rss_query_family_signature:
                family_count = recent_rss_query_family_signatures.count(current_rss_query_family_signature)
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
                        yield synthesized
                        return
                    yield _repeated_semantic_research_message(
                        tool_calls=normalized_tool_calls,
                        history=history,
                    )
                    return
                if (
                    rss_owned_headline_prompt
                    and len(recent_rss_query_family_signatures) >= int(settings.agent_headline_roundup_rss_turn_cap)
                ):
                    _record_loop_event(
                        event_type="headline_roundup_convergence",
                        stage=stage,
                        mode=mode,
                        model=selected_model,
                        details={
                            "turn": turn_number,
                            "family_signature": current_rss_query_family_signature,
                            "rss_turns": len(recent_rss_query_family_signatures),
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
                        yield synthesized
                        return
            if _exploration_churn_detected(recent_exploration_signatures):
                _record_loop_event(
                    event_type="exploration_churn",
                    stage=stage,
                    mode=mode,
                    model=selected_model,
                    details={
                        "turn": turn_number,
                        "recent_signatures": list(recent_exploration_signatures),
                        "window": int(settings.agent_exploration_churn_window),
                        "max_unique_signatures": int(settings.agent_exploration_churn_max_unique_signatures),
                    },
                )
                yield _exploration_churn_message(
                    tool_calls=normalized_tool_calls,
                    history=history,
                )
                return
            log.info(
                "Tool calls executed (streaming turn)",
                turn=turn + 1,
                tools=[_tool_call_name(tc) for tc in normalized_tool_calls],
                model=selected_model,
                mode=mode,
                stage=stage,
            )
        else:
            _log_agent_final_turn(
                turn=turn_number,
                mode=mode,
                stage=stage,
                selected_model=selected_model,
                user_context=user_context,
                content=message.content or "",
            )
            async for token in _stream_final_response(
                history,
                user_context,
                selected_model=selected_model,
                stage=stage,
            ):
                yield token
            return

    yield _max_turns_message(history)
