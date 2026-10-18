"""Byte-exact display budgets; these do not change runtime tool results."""
from __future__ import annotations

import json


TOOL_PREVIEW_SUFFIX = "\n[Tool output preview truncated by Fruitcake; the runtime's tool result is unchanged.]"


def payload_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def text_prefix(text: str, payload: dict, field: str, limit: int, *, suffix: str = "") -> str | None:
    """Find a Unicode-safe prefix that fits after JSON escaping and metadata."""
    if payload_size({**payload, field: suffix}) > limit:
        return None
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if payload_size({**payload, field: text[:middle] + suffix}) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def operation_preview(payload: dict, limit: int) -> dict:
    if payload_size(payload) <= limit or not isinstance(payload.get("text"), str):
        return payload
    original = payload["text"]
    text = original.removesuffix(TOOL_PREVIEW_SUFFIX) if payload.get("preview_truncated") else original
    previous_size = payload.get("original_text_bytes", 0)
    if not isinstance(previous_size, int) or previous_size < 0:
        previous_size = 0
    result = {**payload, "preview_truncated": True,
              "original_text_bytes": max(len(original.encode("utf-8")), previous_size)}
    prefix = text_prefix(text, result, "text", limit, suffix=TOOL_PREVIEW_SUFFIX)
    if prefix is not None:
        result["text"] = prefix + TOOL_PREVIEW_SUFFIX
    return result
