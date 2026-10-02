"""Fix litellm's lossy Ollama chat request translation.

litellm 1.82.2's ``OllamaChatConfig.transform_request`` rebuilds every history
message as role/content/thinking/images only. Two fields are silently dropped
on the way to Ollama's native ``/api/chat``:

- ``tool_calls`` on prior assistant messages (parsed into ``new_tools`` but
  never copied onto the outgoing message), and
- the tool identity on ``role: "tool"`` result messages (``tool_name`` is in
  litellm's own ``OllamaChatCompletionMessage`` TypedDict but never set).

The result is that every multi-turn tool conversation reaches Ollama with
orphaned tool responses — tool results with no preceding tool call. For
strict renderer/parser models (e.g. qwen3.5-family), that transcript is
out-of-distribution and measurably degrades post-tool turns, including the
malformed tool-call output that Ollama rejects with "failed to parse JSON"
(see scripts/diagnose_local_tool_calls.py and
Docs/_internal/qwen_tool_calling_root_cause.md).

This patch wraps ``transform_request`` and re-injects both fields from the
source messages by index (the transform emits exactly one Ollama message per
input message, in order). Each field is only set when absent, so if a litellm
upgrade fixes the upstream bug this patch degrades to a no-op.

It also coalesces system messages into a single leading system message.
The app legitimately produces several (persona context + followup hints at
the front, guardrail/grounding notes inserted mid-history), but models with
strict Jinja chat templates hard-reject any system message that is not
message[0] — reproduced as a deterministic HTTP 400 ("System message must be
at the beginning") from qwen36-heretic:q4km. Merging preserves all note
content in order while satisfying the strictest template contract; models
with lenient templates are unaffected.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

import structlog

log = structlog.get_logger()

_PATCH_FLAG = "_fruitcake_tool_history_patch"
_STREAM_PATCH_FLAG = "_fruitcake_stream_patch"
_OLLAMA_TIMING_FIELDS = (
    "total_duration",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
)


def _attach_ollama_metrics(response: Any, payload: Dict[str, Any]) -> Any:
    """Retain metrics that LiteLLM 1.82 drops from Ollama responses."""
    cached = int(payload.get("prompt_eval_cached_count", 0) or 0)
    usage = getattr(response, "usage", None)
    if usage is not None:
        from litellm.types.utils import PromptTokensDetailsWrapper

        usage.prompt_tokens_details = PromptTokensDetailsWrapper(cached_tokens=max(0, cached))

    metrics = {
        key: max(0, int(payload.get(key, 0) or 0))
        for key in _OLLAMA_TIMING_FIELDS
        if payload.get(key) is not None
    }
    metrics["prompt_eval_cached_count"] = max(0, cached)
    existing = getattr(response, "provider_specific_fields", None)
    provider_fields = dict(existing) if isinstance(existing, dict) else {}
    provider_fields["ollama_metrics"] = metrics
    response.provider_specific_fields = provider_fields
    return response


def apply_litellm_ollama_stream_patch() -> bool:
    """Preserve reasoning and complete-call identity before LiteLLM translation.

    Ollama emits complete argument objects. LiteLLM serializes them and assigns
    indexes starting at zero in each chunk, making separate calls look like
    fragments of one call. Its tag handling also removes reasoning boundaries
    before the provider-neutral normalizer can inspect them.
    """
    from litellm.llms.ollama.chat.transformation import OllamaChatCompletionResponseIterator

    from app.agent.model_stream import ModelStreamNormalizer

    iterator_class = OllamaChatCompletionResponseIterator
    if getattr(iterator_class, _STREAM_PATCH_FLAG, False):
        return False
    original_parser = iterator_class.chunk_parser

    def chunk_parser_with_stream_boundaries(self, chunk):
        # The upstream parser mutates tool dictionaries. Preserve the caller's
        # raw chunk, including reasoning, while retaining its usage/error logic.
        translated_chunk = deepcopy(chunk)
        message = translated_chunk["message"]
        raw_content = message.get("content")
        raw_reasoning = message.pop("thinking", None)
        message["content"] = ""

        next_index = getattr(self, "_fruitcake_next_tool_index", 0)
        for call in message.get("tool_calls") or []:
            if isinstance((call.get("function") or {}).get("arguments"), dict):
                call["index"] = next_index
                next_index += 1
        self._fruitcake_next_tool_index = next_index

        response = _attach_ollama_metrics(original_parser(self, translated_chunk), chunk)
        normalizer = getattr(self, "_fruitcake_reasoning_normalizer", None)
        if normalizer is None:
            normalizer = ModelStreamNormalizer()
            self._fruitcake_reasoning_normalizer = normalizer
        events = normalizer.feed({"choices": [{
            "delta": {"content": raw_content, "reasoning_content": raw_reasoning},
            "finish_reason": "stop" if chunk.get("done") else None,
        }]})
        delta = response.choices[0].delta
        delta.content = "".join(event.text or "" for event in events if event.kind == "text_delta") or None
        delta.reasoning_content = "".join(
            event.text or "" for event in events if event.kind == "reasoning_delta"
        ) or None
        return response

    iterator_class.chunk_parser = chunk_parser_with_stream_boundaries
    setattr(iterator_class, _STREAM_PATCH_FLAG, True)
    log.info("litellm_ollama_stream_patch_installed")
    return True


def _coerce_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _source_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        converted.append(
            {"function": {"name": name, "arguments": _coerce_arguments(function.get("arguments"))}}
        )
    return converted


def _coalesce_system_messages(messages: List[Any]) -> List[Any]:
    """Merge every system message into one leading system message.

    Strict Jinja chat templates (e.g. qwen36-heretic:q4km) raise a hard 400
    for any system message that is not messages[0]. Content is joined in
    original order; non-system messages keep their relative order.
    """
    system_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and str(message.get("role") or "") == "system"
    ]
    if not system_indices or (len(system_indices) == 1 and system_indices[0] == 0):
        return messages

    contents = [
        part
        for index in system_indices
        if (part := str(messages[index].get("content") or "").strip())
    ]
    merged = dict(messages[system_indices[0]])
    merged["content"] = "\n\n".join(contents)
    rest = [
        message
        for index, message in enumerate(messages)
        if index not in set(system_indices)
    ]
    return [merged, *rest]


def apply_litellm_ollama_tool_history_patch() -> bool:
    """Install the patch. Returns True if newly installed, False if already active."""
    from litellm.llms.ollama.chat.transformation import OllamaChatConfig

    if getattr(OllamaChatConfig, _PATCH_FLAG, False):
        return False

    original_transform = OllamaChatConfig.transform_request
    original_transform_response = OllamaChatConfig.transform_response

    def transform_request_with_tool_history(
        self: OllamaChatConfig,
        model: str,
        messages: List[Any],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        # Snapshot tool history before the original transform runs — it
        # mutates source dicts in place, so read what we need first.
        snapshots: List[Dict[str, Any]] = []
        call_id_to_name: Dict[str, str] = {}
        for message in messages:
            if hasattr(message, "model_dump"):
                message = message.model_dump(exclude_none=True)
            if not isinstance(message, dict):
                snapshots.append({})
                continue
            role = str(message.get("role") or "")
            snapshot: Dict[str, Any] = {"role": role}
            if role == "assistant" and message.get("tool_calls"):
                snapshot["tool_calls"] = _source_tool_calls(message)
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "").strip()
                    name = str(((call.get("function") or {}).get("name")) or "").strip()
                    if call_id and name:
                        call_id_to_name[call_id] = name
            elif role == "tool":
                snapshot["tool_call_id"] = str(message.get("tool_call_id") or "").strip()
            snapshots.append(snapshot)

        data = original_transform(self, model, messages, optional_params, litellm_params, headers)

        outgoing = data.get("messages")
        if not isinstance(outgoing, list) or len(outgoing) != len(snapshots):
            # Unexpected shape from a litellm change — leave the request as-is
            # rather than guessing at alignment.
            log.warning(
                "litellm_ollama_patch_alignment_skipped",
                source_count=len(snapshots),
                outgoing_count=len(outgoing) if isinstance(outgoing, list) else None,
            )
            return data

        for snapshot, out_message in zip(snapshots, outgoing):
            if not isinstance(out_message, dict):
                continue
            tool_calls = snapshot.get("tool_calls")
            if tool_calls and not out_message.get("tool_calls"):
                out_message["tool_calls"] = tool_calls
            if snapshot.get("role") == "tool" and not out_message.get("tool_name"):
                name = call_id_to_name.get(str(snapshot.get("tool_call_id") or ""))
                if name:
                    out_message["tool_name"] = name

        data["messages"] = _coalesce_system_messages(outgoing)
        return data

    def transform_response_with_metrics(self, *args, **kwargs):
        raw_response = kwargs.get("raw_response")
        if raw_response is None and len(args) >= 2:
            raw_response = args[1]
        response = original_transform_response(self, *args, **kwargs)
        try:
            payload = raw_response.json() if raw_response is not None else {}
        except Exception:
            payload = {}
        return _attach_ollama_metrics(response, payload if isinstance(payload, dict) else {})

    OllamaChatConfig.transform_request = transform_request_with_tool_history  # type: ignore[method-assign]
    OllamaChatConfig.transform_response = transform_response_with_metrics  # type: ignore[method-assign]
    setattr(OllamaChatConfig, _PATCH_FLAG, True)
    log.info("litellm_ollama_tool_history_patch_installed")
    return True
