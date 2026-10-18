import json

import pytest

from app.coding_runtime.output import TOOL_PREVIEW_SUFFIX, operation_preview, payload_size, text_prefix
from app.coding_runtime.pi_rpc import _normalize_event


@pytest.mark.parametrize("text", ["x" * 32768, '\\"\n\t\x00' * 10000, "\U0001f600\u4e2d\u00e9" * 10000])
def test_tool_preview_accounts_for_json_escaping_metadata_and_utf8(text):
    payload = {"operation_id": "call-1", "tool": "read", "text": text, "is_error": False}
    preview = operation_preview(payload, 32768)
    assert payload_size(preview) <= 32768
    assert preview["preview_truncated"] is True
    assert preview["original_text_bytes"] == len(text.encode())
    assert preview["text"].endswith(TOOL_PREVIEW_SUFFIX)
    assert text.startswith(preview["text"].removesuffix(TOOL_PREVIEW_SUFFIX))
    assert preview["operation_id"] == "call-1"
    assert json.loads(json.dumps(preview, ensure_ascii=False).encode()) == preview
    assert payload["text"] == text


def test_small_preview_is_unchanged_and_impossible_metadata_is_not_hidden():
    small = {"operation_id": "1", "text": "A small file"}
    assert operation_preview(small, 32768) == small
    huge = {"operation_id": "x" * 40000, "text": "file"}
    assert payload_size(operation_preview(huge, 32768)) > 32768
    assert text_prefix("file", huge, "text", 32768) is None


@pytest.mark.parametrize("record_type", ["tool_execution_update", "tool_execution_end"])
def test_adapter_marks_its_own_tool_preview_truncation(record_type):
    original = "x" * 100000
    body = {"content": [{"type": "text", "text": original}]}
    record = {"type": record_type, "toolCallId": "call-1", "toolName": "read",
              "partialResult" if record_type.endswith("update") else "result": body}
    event = _normalize_event(record)
    assert event.payload["preview_truncated"] is True
    assert event.payload["original_text_bytes"] == 100000
    assert len(event.payload["text"].encode()) <= 32768
    assert body["content"][0]["text"] == original
    bounded = operation_preview(event.payload, 32768)
    assert payload_size(bounded) <= 32768
    assert bounded["text"].count(TOOL_PREVIEW_SUFFIX) == 1
    assert bounded["original_text_bytes"] == 100000


def test_large_visible_answer_is_not_silently_clipped_by_the_adapter():
    text = "x" * 50000
    message = _normalize_event({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "thinking", "thinking": "PRIVATE"}, {"type": "text", "text": text}],
    }})
    delta = _normalize_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}})
    assert message.payload["text"] == text
    assert delta.payload["delta"] == text
    assert "PRIVATE" not in json.dumps(message.payload)
