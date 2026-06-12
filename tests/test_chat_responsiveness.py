from __future__ import annotations

import pytest
from unittest.mock import patch
from sqlalchemy import select

from app.api.chat import (
    _apply_workspace_followup_grounding,
    _is_recent_workspace_followup_prompt,
    _load_history,
    _load_recent_workspace_context,
    _record_chat_stage_timing,
)
from app.agent.core import _recent_rss_evidence
from app.db.models import ChatMessage, ChatSession
from app.metrics import _Metrics
from tests.conftest import TestSessionLocal


def test_record_chat_stage_timing_records_elapsed_ms():
    stage_timings = {}
    with patch("app.api.chat.metrics", new=_Metrics()):
        with patch("app.api.chat.time.perf_counter", return_value=10.025):
            _record_chat_stage_timing(stage_timings, "history_load", 10.0)
        assert stage_timings["history_load"] == 25.0


@pytest.mark.asyncio
async def test_load_history_compacts_older_chat_messages(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Compaction test")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="A" * 120),
                ChatMessage(session_id=session.id, role="assistant", content="B" * 120),
                ChatMessage(session_id=session.id, role="user", content="Latest question"),
                ChatMessage(session_id=session.id, role="assistant", content="Latest answer"),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)
        marker_rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session.id, ChatMessage.role == "system")
                .order_by(ChatMessage.id.desc())
            )
        ).scalars().all()

    assert history[0]["role"] == "system"
    assert "Earlier chat history was compacted" in history[0]["content"]
    assert history[-2:] == [
        {"role": "user", "content": "Latest question"},
        {"role": "assistant", "content": "Latest answer"},
    ]
    assert len(marker_rows) == 1
    assert "\"kind\": \"history_compaction\"" in (marker_rows[0].tool_results or "")


@pytest.mark.asyncio
async def test_load_history_returns_full_history_when_under_budget(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 5000)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Small history")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="Hello"),
                ChatMessage(session_id=session.id, role="assistant", content="Hi there"),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)

    assert history == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


@pytest.mark.asyncio
async def test_load_history_preserves_tool_metadata(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 5000)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    assistant_tool_calls = [
        {
            "id": "rss_1",
            "function": {
                "name": "list_recent_feed_items",
                "arguments": "{\"max_results\": 10}",
            },
        }
    ]

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Tool history")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="What are the headlines this evening?"),
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"rss_1","function":{"name":"list_recent_feed_items","arguments":"{\\"max_results\\": 10}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Recent feed items (10):\n\n[1] Story A\n[2] Story B",
                    tool_results='{"tool_call_id":"rss_1"}',
                ),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)

    assert history[1]["tool_calls"] == assistant_tool_calls
    assert history[2]["tool_call_id"] == "rss_1"


@pytest.mark.asyncio
async def test_load_history_compaction_preserves_recent_tool_metadata(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 3)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Compaction with tools")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="A" * 120),
                ChatMessage(session_id=session.id, role="assistant", content="B" * 120),
                ChatMessage(session_id=session.id, role="user", content="What are the headlines this evening?"),
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"recent_1","function":{"name":"list_recent_feed_items","arguments":"{\\"max_results\\": 10, \\"window\\": {\\"mode\\": \\"hours\\", \\"value\\": 12}}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Recent feed items (10):\n\n[1] Story A\n[2] Story B",
                    tool_results='{"tool_call_id":"recent_1"}',
                ),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)

    assert history[0]["role"] == "system"
    assert history[-2]["tool_calls"][0]["function"]["name"] == "list_recent_feed_items"
    assert history[-1]["tool_call_id"] == "recent_1"

    evidence = _recent_rss_evidence(history)
    assert len(evidence) == 1
    assert evidence[0]["tool_name"] == "list_recent_feed_items"
    assert evidence[0]["item_count"] == 2


@pytest.mark.asyncio
async def test_get_session_exposes_compaction_events_without_polluting_messages(client):
    await client.post(
        "/auth/register",
        json={
            "username": "chatcompactuser",
            "email": "chatcompact@example.com",
            "password": "pass123",
        },
    )
    login = await client.post("/auth/login", json={"username": "chatcompactuser", "password": "pass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post("/chat/sessions", json={"title": "Compacted Session"}, headers=headers)
    session_id = create.json()["id"]

    async with TestSessionLocal() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="U1"),
                ChatMessage(session_id=session.id, role="assistant", content="A1"),
                ChatMessage(
                    session_id=session.id,
                    role="system",
                    content="Earlier chat history was compacted to keep the live context focused.",
                    tool_results='{"kind":"history_compaction","compacted_until_message_id":2,"compacted_message_count":2,"estimated_tokens_before":120,"estimated_tokens_after":30,"recent_messages_kept":2}',
                ),
                ChatMessage(session_id=session.id, role="user", content="U2"),
                ChatMessage(session_id=session.id, role="assistant", content="A2"),
            ]
        )
        await db.commit()

    resp = await client.get(f"/chat/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "user", "assistant"]
    assert len(payload["compaction_events"]) == 1
    assert payload["compaction_events"][0]["kind"] == "history_compaction"
    assert payload["compaction_events"][0]["compacted_until_message_id"] == 2


@pytest.mark.asyncio
async def test_recent_workspace_context_prefers_latest_workspace_file(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.workspace_dir", "/tmp/workspace")

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=7, title="Workspace follow-up")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"write_1","function":{"name":"write_file","arguments":"{\\"path\\":\\"workspace/7/reports/key_points.md\\",\\"content\\":\\"hello\\"}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Wrote 5 bytes to reports/key_points.md",
                    tool_results='{"tool_call_id":"write_1"}',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"read_1","function":{"name":"read_file","arguments":"{\\"path\\":\\"reports/key_points.md\\"}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Current notes",
                    tool_results='{"tool_call_id":"read_1"}',
                ),
            ]
        )
        await db.commit()

        context = await _load_recent_workspace_context(session.id, user_id=7, db=db)

    assert context.active_path == "reports/key_points.md"
    assert context.recent_write_path == "reports/key_points.md"
    assert context.recent_read_path == "reports/key_points.md"
    assert _is_recent_workspace_followup_prompt("append this to the key points doc", context) is True
    grounded = _apply_workspace_followup_grounding(
        [{"role": "user", "content": "append this to the key points doc"}],
        user_prompt="append this to the key points doc",
        context=context,
    )
    assert grounded[0]["role"] == "system"
    assert "Active workspace file: reports/key_points.md" in grounded[0]["content"]


@pytest.mark.asyncio
async def test_load_history_compaction_preserves_workspace_continuity(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.workspace_dir", "/tmp/workspace")
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=11, title="Workspace compaction")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="Please create a key points file for this review."),
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"write_ctx","function":{"name":"write_file","arguments":"{\\"path\\":\\"workspace/11/reports/key_points.md\\",\\"content\\":\\"start\\"}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Wrote 5 bytes to reports/key_points.md",
                    tool_results='{"tool_call_id":"write_ctx"}',
                ),
                ChatMessage(session_id=session.id, role="assistant", content="Created the working file."),
                ChatMessage(session_id=session.id, role="user", content="A" * 180),
                ChatMessage(session_id=session.id, role="assistant", content="B" * 180),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)
        marker_rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session.id, ChatMessage.role == "system")
                .order_by(ChatMessage.id.desc())
            )
        ).scalars().all()

    assert history[0]["role"] == "system"
    assert "Operational continuity:" in history[0]["content"]
    assert "Active workspace file: reports/key_points.md" in history[0]["content"]
    assert "Pending objective: Please create a key points file for this review." in history[0]["content"]
    assert '"active_workspace_file": "reports/key_points.md"' in (marker_rows[0].tool_results or "")


@pytest.mark.asyncio
async def test_load_history_recompaction_carries_forward_prior_recap(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Recompaction test")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="alpha_topic " + "A" * 120),
                ChatMessage(session_id=session.id, role="assistant", content="alpha_answer " + "B" * 120),
                ChatMessage(session_id=session.id, role="user", content="First follow-up"),
                ChatMessage(session_id=session.id, role="assistant", content="First answer"),
            ]
        )
        await db.commit()

        first_history = await _load_history(session.id, db)
        await db.commit()
        assert "alpha_topic" in first_history[0]["content"]

        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="bravo_topic " + "C" * 120),
                ChatMessage(session_id=session.id, role="assistant", content="bravo_answer " + "D" * 120),
                ChatMessage(session_id=session.id, role="user", content="Second follow-up"),
                ChatMessage(session_id=session.id, role="assistant", content="Second answer"),
            ]
        )
        await db.commit()

        second_history = await _load_history(session.id, db)
        await db.commit()

        marker_rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session.id, ChatMessage.role == "system")
                .order_by(ChatMessage.id.desc())
            )
        ).scalars().all()

    boundary = second_history[0]["content"]
    # The newest compacted window is summarized…
    assert "bravo_topic" in boundary
    # …and the recap from the first compaction is carried forward, not lost.
    assert "alpha_topic" in boundary
    assert "Previously compacted" in boundary
    assert len(marker_rows) == 1
    assert '"carried_recap"' in (marker_rows[0].tool_results or "")
    assert "alpha_topic" in (marker_rows[0].tool_results or "")


@pytest.mark.asyncio
async def test_load_history_compaction_recap_includes_messages_near_cut(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 2)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Recap sampling test")
        db.add(session)
        await db.flush()
        rows = []
        for index in range(14):
            rows.append(ChatMessage(session_id=session.id, role="user", content=f"subject_marker_{index} " + "U" * 80))
            rows.append(ChatMessage(session_id=session.id, role="assistant", content=f"answer_marker_{index} " + "A" * 80))
        rows.append(ChatMessage(session_id=session.id, role="user", content="Latest question"))
        rows.append(ChatMessage(session_id=session.id, role="assistant", content="Latest answer"))
        db.add_all(rows)
        await db.commit()

        history = await _load_history(session.id, db)
        await db.commit()

    boundary = history[0]["content"]
    # Early framing context survives…
    assert "subject_marker_0" in boundary
    # …and so does the context nearest the cut, not only the oldest turns.
    assert "answer_marker_13" in boundary


@pytest.mark.asyncio
async def test_load_history_compaction_keeps_tool_chain_with_results(monkeypatch):
    monkeypatch.setattr("app.api.chat.settings.chat_history_soft_token_limit", 40)
    monkeypatch.setattr("app.api.chat.settings.chat_recent_messages_keep", 1)

    async with TestSessionLocal() as db:
        session = ChatSession(user_id=1, title="Tool chain split test")
        db.add(session)
        await db.flush()
        db.add_all(
            [
                ChatMessage(session_id=session.id, role="user", content="A" * 120),
                ChatMessage(session_id=session.id, role="assistant", content="B" * 120),
                ChatMessage(session_id=session.id, role="user", content="What are the headlines this evening?"),
                ChatMessage(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    tool_calls='[{"id":"recent_1","function":{"name":"list_recent_feed_items","arguments":"{}"}}]',
                ),
                ChatMessage(
                    session_id=session.id,
                    role="tool",
                    content="Recent feed items (2):\n\n[1] Story A\n[2] Story B",
                    tool_results='{"tool_call_id":"recent_1"}',
                ),
            ]
        )
        await db.commit()

        history = await _load_history(session.id, db)
        await db.commit()

    assert history[0]["role"] == "system"
    # The cut snaps back so the assistant tool call stays with its tool result.
    assert history[-2]["tool_calls"][0]["id"] == "recent_1"
    assert history[-1]["tool_call_id"] == "recent_1"
