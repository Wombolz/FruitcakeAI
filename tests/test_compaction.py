"""
FruitcakeAI v5 — Shared compaction module tests

Covers the format/construction layer in app/agent/compaction.py used by both
persisted chat compaction (app/api/chat.py) and runtime projection
(app/agent/core.py): recap selection, boundary payload construction, and the
single boundary renderer.
"""

from __future__ import annotations

from app.agent.compaction import (
    CHAT_COMPACTION_BOUNDARY_HEADER,
    COMPACTION_BOUNDARY_HEADERS,
    COMPACTION_MARKER_KIND,
    COMPACTION_SCHEMA_VERSION,
    RUNTIME_COMPACTION_BOUNDARY_HEADER,
    build_boundary_payload,
    is_compaction_boundary_text,
    recap_lines_from_marker,
    recap_summaries,
    render_boundary_text,
    select_recap_messages,
)


def _turns(count: int) -> list[dict]:
    messages = []
    for index in range(count):
        messages.append({"role": "user", "content": f"question_{index}"})
        messages.append({"role": "assistant", "content": f"answer_{index}"})
    return messages


def test_select_recap_messages_keeps_head_and_near_cut_tail():
    prefix = _turns(20)
    selected = select_recap_messages(prefix, head_count=3, tail_count=7)
    contents = [message["content"] for message in selected]
    assert len(selected) == 10
    assert contents[:3] == ["question_0", "answer_0", "question_1"]
    assert contents[-1] == "answer_19"


def test_select_recap_messages_prefers_high_signal_over_tool_chatter():
    prefix = _turns(2)
    for index in range(10):
        prefix.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{index}", "function": {"name": "search", "arguments": "{}"}}],
            }
        )
        prefix.append({"role": "tool", "tool_call_id": f"c{index}", "content": f"result_{index}"})
    prefix.append({"role": "user", "content": "final_user_question"})
    prefix.append({"role": "assistant", "content": "final_conclusion"})

    selected = select_recap_messages(prefix, head_count=3, tail_count=4)
    contents = [message.get("content") for message in selected]
    assert "final_user_question" in contents
    assert "final_conclusion" in contents


def test_recap_summaries_render_deterministically():
    prefix = _turns(8)
    first = recap_summaries(prefix, head_count=3, tail_count=7, max_lines=10)
    second = recap_summaries(prefix, head_count=3, tail_count=7, max_lines=10)
    assert first == second
    assert len(first) <= 10


def test_boundary_payload_is_versioned():
    payload = build_boundary_payload(mode="chat", recap=["User: hi"])
    assert payload["version"] == COMPACTION_SCHEMA_VERSION
    assert payload["kind"] == COMPACTION_MARKER_KIND
    assert payload["mode"] == "chat"
    assert payload["tool_state"] == []


def test_renderer_includes_sections_only_when_present():
    payload = build_boundary_payload(
        mode="chat",
        recap=["User: current question"],
        carried_recap=["User: older question"],
        continuity={"active_workspace_file": "notes.md", "pending_objective": "finish notes"},
        tool_state=["last search: 3 results"],
    )
    text = render_boundary_text(payload)
    assert text.startswith(CHAT_COMPACTION_BOUNDARY_HEADER)
    assert "Previously compacted (older context):" in text
    assert "- User: older question" in text
    assert "Recently compacted turns:" in text
    assert "- User: current question" in text
    assert "Operational continuity:" in text
    assert "- Active workspace file: notes.md" in text
    assert "- Pending objective: finish notes" in text
    assert "Tool state:" in text
    assert "- last search: 3 results" in text

    bare = render_boundary_text(build_boundary_payload(mode="chat", recap=["User: hi"]))
    assert "Previously compacted" not in bare
    assert "Recently compacted turns:" not in bare
    assert "Operational continuity:" not in bare
    assert "Tool state:" not in bare


def test_renderer_runtime_mode_matches_runtime_format():
    text = render_boundary_text(build_boundary_payload(mode="runtime", recap=["Tool search: hit"]))
    assert text.splitlines()[0] == RUNTIME_COMPACTION_BOUNDARY_HEADER
    assert "Preserve these compacted facts unless later context contradicts them:" in text
    assert "- Tool search: hit" in text


def test_renderer_is_deterministic():
    payload = build_boundary_payload(mode="chat", recap=["User: hi"], carried_recap=["User: old"])
    assert render_boundary_text(payload) == render_boundary_text(payload)


def test_legacy_headers_remain_recognized():
    # Persisted markers keep the header they were written with forever, so
    # every header ever rendered must stay recognized.
    for header in COMPACTION_BOUNDARY_HEADERS:
        assert is_compaction_boundary_text(header + "\nmore text")
    assert not is_compaction_boundary_text("A normal system prompt.")


def test_recap_lines_from_marker_prefers_structured_payload():
    payload = {"recap": ["User: structured line"]}
    assert recap_lines_from_marker(payload, "ignored content") == ["User: structured line"]


def test_recap_lines_from_marker_parses_legacy_content():
    legacy_content = "\n".join(
        [
            CHAT_COMPACTION_BOUNDARY_HEADER,
            "Use this as a compact recap unless newer turns contradict it:",
            "- User: legacy line one",
            "- Assistant: legacy line two",
            "Operational continuity:",
            "- Active workspace file: skip-me.md",
        ]
    )
    lines = recap_lines_from_marker({}, legacy_content)
    assert lines == ["User: legacy line one", "Assistant: legacy line two"]
