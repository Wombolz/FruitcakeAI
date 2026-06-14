"""
FruitcakeAI v5 — Shared compaction boundary format helpers.

Used by both compaction layers:
- persisted chat-session compaction in app/api/chat.py
- per-model-call runtime projection in app/agent/core.py

This module owns format and construction only: token estimation, recap
selection and summarization, the structured boundary payload, and the single
boundary renderer. Compaction policy stays in the owning layers — when to
compact, what to persist, cut-point snapping, diagnostics, and continuity or
tool-state discovery all remain layer-local.
"""

from __future__ import annotations

from typing import Any, Dict, List

ESTIMATED_CHARS_PER_TOKEN = 4

COMPACTION_SCHEMA_VERSION = 1
COMPACTION_MARKER_KIND = "history_compaction"

# Boundary headers, by payload mode. Runtime projection recognizes boundary
# messages by their first line, and persisted chat markers keep whatever
# header they were written with, so every header that was ever rendered must
# stay in COMPACTION_BOUNDARY_HEADERS permanently — dropping one silently
# strips re-compaction protection from existing sessions.
RUNTIME_COMPACTION_BOUNDARY_HEADER = "Earlier conversation was compacted to stay within the context budget."
CHAT_COMPACTION_BOUNDARY_HEADER = "Earlier chat history was compacted to keep the live context focused."
COMPACTION_BOUNDARY_HEADERS = (
    RUNTIME_COMPACTION_BOUNDARY_HEADER,
    CHAT_COMPACTION_BOUNDARY_HEADER,
)

_MODE_HEADERS = {
    "chat": CHAT_COMPACTION_BOUNDARY_HEADER,
    "runtime": RUNTIME_COMPACTION_BOUNDARY_HEADER,
}
_MODE_INSTRUCTIONS = {
    "chat": "Use this as a compact recap unless newer turns contradict it:",
    "runtime": "Preserve these compacted facts unless later context contradicts them:",
}
_MODE_EMPTY_RECAP = {
    "chat": "Earlier user and assistant turns were compacted.",
    "runtime": "Earlier tool and assistant turns were compacted.",
}


def estimate_tokens(text: str) -> int:
    chars = len(str(text or ""))
    return max(1, chars // ESTIMATED_CHARS_PER_TOKEN) if chars else 0


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(((call.get("function") or {}).get("name") or "")).strip()
    return str(getattr(getattr(call, "function", None), "name", "") or "").strip()


def _tool_call_arguments_text(call: Any) -> str:
    if isinstance(call, dict):
        raw = (call.get("function") or {}).get("arguments")
    else:
        raw = getattr(getattr(call, "function", None), "arguments", None)
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    total = estimate_tokens(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(_tool_call_name(call))
        total += estimate_tokens(_tool_call_arguments_text(call))
    return total


def estimate_history_tokens(history: List[Dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in history)


def compact_text(value: str, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def is_high_signal_recap_message(message: Dict[str, Any]) -> bool:
    role = str(message.get("role") or "")
    if role == "user":
        return True
    return role == "assistant" and not message.get("tool_calls")


def select_recap_messages(
    prefix: List[Dict[str, Any]],
    *,
    head_count: int,
    tail_count: int,
) -> List[Dict[str, Any]]:
    """Keep early framing turns plus the turns nearest the cut, preferring
    user messages and assistant conclusions over tool chatter."""
    if len(prefix) <= head_count + tail_count:
        return list(prefix)
    head = prefix[:head_count]
    rest = prefix[head_count:]
    selected = {
        index
        for index in [i for i, message in enumerate(rest) if is_high_signal_recap_message(message)][-tail_count:]
    }
    for index in range(len(rest) - 1, -1, -1):
        if len(selected) >= tail_count:
            break
        selected.add(index)
    tail = [rest[index] for index in sorted(selected)]
    return head + tail


def compact_message_summary(
    message: Dict[str, Any],
    *,
    tool_name_lookup: dict[str, str] | None = None,
) -> str:
    role = str(message.get("role") or "").strip() or "unknown"
    if role == "tool" and tool_name_lookup is not None:
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        tool_name = tool_name_lookup.get(tool_call_id) or "unknown_tool"
        content = compact_text(str(message.get("content") or ""), max_chars=140)
        return f"Tool {tool_name}: {content}"
    if role == "assistant" and message.get("tool_calls"):
        tool_names = [
            _tool_call_name(call) or "unknown_tool"
            for call in (message.get("tool_calls") or [])
        ]
        if tool_names:
            return f"Assistant requested tools: {', '.join(tool_names[:5])}"
    content = compact_text(str(message.get("content") or ""), max_chars=160)
    return f"{role.capitalize()}: {content}" if content else f"{role.capitalize()}:"


def recap_summaries(
    prefix: List[Dict[str, Any]],
    *,
    head_count: int,
    tail_count: int,
    max_lines: int,
    tool_name_lookup: dict[str, str] | None = None,
) -> list[str]:
    summaries: list[str] = []
    for message in select_recap_messages(prefix, head_count=head_count, tail_count=tail_count):
        summary = compact_message_summary(message, tool_name_lookup=tool_name_lookup)
        if summary:
            summaries.append(summary)
        if len(summaries) >= max_lines:
            break
    return summaries


def condense_carried_recap(lines: List[str], *, max_lines: int = 6) -> list[str]:
    cleaned = [str(line).strip() for line in lines if str(line).strip()]
    if len(cleaned) <= max_lines:
        return cleaned
    return cleaned[:2] + cleaned[-(max_lines - 2):]


def recap_lines_from_marker(payload: Dict[str, Any], content: str) -> list[str]:
    """Recover the recap lines from an existing marker, preferring structured
    metadata and falling back to parsing legacy marker content."""
    recap = payload.get("recap")
    if isinstance(recap, list) and recap:
        return [str(item).strip() for item in recap if str(item).strip()]
    lines: list[str] = []
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Operational continuity:"):
            break
        if stripped.startswith("- "):
            lines.append(stripped[2:].strip())
    return [line for line in lines if line]


def build_boundary_payload(
    *,
    mode: str,
    recap: List[str] | None = None,
    carried_recap: List[str] | None = None,
    continuity: Dict[str, Any] | None = None,
    tool_state: List[str] | None = None,
) -> Dict[str, Any]:
    """Build the versioned structured payload describing a compaction
    boundary. tool_state is reserved in schema v1; neither layer populates
    it yet."""
    return {
        "version": COMPACTION_SCHEMA_VERSION,
        "kind": COMPACTION_MARKER_KIND,
        "mode": mode if mode in _MODE_HEADERS else "chat",
        "recap": [str(item).strip() for item in (recap or []) if str(item).strip()],
        "carried_recap": [str(item).strip() for item in (carried_recap or []) if str(item).strip()],
        "continuity": dict(continuity or {}),
        "tool_state": [str(item).strip() for item in (tool_state or []) if str(item).strip()],
    }


def _continuity_lines(continuity: Dict[str, Any]) -> list[str]:
    lines: list[str] = []
    active_workspace_file = str(continuity.get("active_workspace_file") or "").strip()
    active_document = str(continuity.get("active_document") or "").strip()
    pending_objective = str(continuity.get("pending_objective") or "").strip()
    recent_actions = [
        str(item).strip()
        for item in (continuity.get("recent_actions") or [])
        if str(item).strip()
    ]
    if active_workspace_file:
        lines.append(f"- Active workspace file: {active_workspace_file}")
    if active_document:
        lines.append(f"- Active document target: {active_document}")
    if pending_objective:
        lines.append(f"- Pending objective: {pending_objective}")
    if recent_actions:
        lines.append(f"- Recent tool actions: {', '.join(recent_actions[:4])}")
    return lines


def render_boundary_text(payload: Dict[str, Any]) -> str:
    """Render a boundary payload to the model-facing system-message text.
    This is the only place boundary prose is produced — both layers must
    render through here so the format cannot drift."""
    mode = str(payload.get("mode") or "chat")
    if mode not in _MODE_HEADERS:
        mode = "chat"
    lines = [_MODE_HEADERS[mode], _MODE_INSTRUCTIONS[mode]]
    carried = [str(item).strip() for item in (payload.get("carried_recap") or []) if str(item).strip()]
    if carried:
        lines.append("Previously compacted (older context):")
        lines.extend(f"- {item}" for item in carried)
        lines.append("Recently compacted turns:")
    recap = [str(item).strip() for item in (payload.get("recap") or []) if str(item).strip()]
    if not recap:
        recap = [_MODE_EMPTY_RECAP[mode]]
    lines.extend(f"- {item}" for item in recap)
    continuity_lines = _continuity_lines(dict(payload.get("continuity") or {}))
    if continuity_lines:
        lines.append("Operational continuity:")
        lines.extend(continuity_lines)
    tool_state = [str(item).strip() for item in (payload.get("tool_state") or []) if str(item).strip()]
    if tool_state:
        lines.append("Tool state:")
        lines.extend(f"- {item}" for item in tool_state)
    return "\n".join(lines)


def boundary_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"role": "system", "content": render_boundary_text(payload)}


def is_compaction_boundary_text(content: str) -> bool:
    return str(content or "").lstrip().startswith(COMPACTION_BOUNDARY_HEADERS)
