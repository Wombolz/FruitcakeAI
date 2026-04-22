from __future__ import annotations

import pytest
from unittest.mock import patch
from sqlalchemy import select

from app.api.chat import _load_history, _record_chat_stage_timing
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
