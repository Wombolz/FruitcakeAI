from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.context import UserContext
from app.agent.core import run_agent, stream_agent
from app.autonomy.planner import _generate_plan_steps
from app.db.models import ChatSession, LLMUsageEvent, Task, User
from app.llm_usage import bind_llm_usage_context, reset_llm_usage_context
from tests.conftest import TestSessionLocal


class _FakeMessage:
    def __init__(self, *, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none: bool = True):
        payload = {"content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        return payload


def _fake_response(*, content: str, model: str = "gpt-4o", prompt_tokens: int = 100, completion_tokens: int = 25):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        choices=[
            SimpleNamespace(
                message=_FakeMessage(content=content, tool_calls=[]),
                finish_reason="stop",
            )
        ],
    )


def _fake_tool_response(
    *,
    tool_name: str,
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 25,
):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        choices=[
            SimpleNamespace(
                message=_FakeMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": "{}"},
                        }
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
    )


async def _fake_stream_with_usage(*parts: str):
    for part in parts:
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=part), finish_reason=None)]
        )
    yield SimpleNamespace(
        model="gpt-4o",
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150),
        choices=[],
    )


async def _seed_user_and_session() -> tuple[int, int]:
    async with TestSessionLocal() as db:
        user = User(
            username="tester",
            email="tester@example.com",
            hashed_password="x",
            role="parent",
            persona="family_assistant",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        session = ChatSession(user_id=user.id, persona="family_assistant", llm_model="gpt-4o")
        db.add(session)
        await db.commit()
        return int(user.id), int(session.id)


async def _seed_task(user_id: int) -> int:
    async with TestSessionLocal() as db:
        task = Task(
            user_id=user_id,
            title="Test task",
            instruction="Do the thing",
            status="pending",
            task_type="one_shot",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return int(task.id)


@pytest.mark.asyncio
async def test_run_agent_records_usage_event():
    user_id, session_id = await _seed_user_and_session()
    user_context = UserContext(user_id=user_id, username="tester", role="parent", persona="family_assistant")

    token = bind_llm_usage_context(user_id=user_id, session_id=session_id, source="chat_rest")
    try:
        with (
            patch("app.agent.core.get_tools_for_user", return_value=[]),
            patch("app.agent.core.litellm.acompletion", new=AsyncMock(return_value=_fake_response(content="Hello"))),
            patch("app.llm_usage.AsyncSessionLocal", new=TestSessionLocal),
            patch("app.llm_usage.litellm.completion_cost", return_value=0.0025),
        ):
            result = await run_agent([{"role": "user", "content": "Hi"}], user_context, stage="chat_simple")
    finally:
        reset_llm_usage_context(token)

    assert result == "Hello"
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(LLMUsageEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == user_id
    assert rows[0].session_id == session_id
    assert rows[0].source == "chat_rest"
    assert rows[0].stage == "chat_simple"
    assert rows[0].prompt_tokens == 100
    assert rows[0].completion_tokens == 25
    assert rows[0].total_tokens == 125
    assert rows[0].estimated_cost_usd == 0.0025


@pytest.mark.asyncio
async def test_stream_agent_records_probe_and_stream_usage_events():
    user_id, session_id = await _seed_user_and_session()
    user_context = UserContext(user_id=user_id, username="tester", role="parent", persona="family_assistant")

    async def _acompletion(**kwargs):
        if kwargs.get("stream"):
            return _fake_stream_with_usage("Hello", " world")
        return _fake_response(content="Hello world", prompt_tokens=80, completion_tokens=20)

    token = bind_llm_usage_context(user_id=user_id, session_id=session_id, source="chat_websocket")
    try:
        with (
            patch("app.agent.core.get_tools_for_user", return_value=[]),
            patch("app.agent.core.litellm.acompletion", side_effect=_acompletion),
            patch("app.llm_usage.AsyncSessionLocal", new=TestSessionLocal),
            patch("app.llm_usage.litellm.completion_cost", return_value=0.003),
        ):
            chunks = [chunk async for chunk in stream_agent([{"role": "user", "content": "Hi"}], user_context, stage="chat_simple")]
    finally:
        reset_llm_usage_context(token)

    assert chunks == ["Hello", " world"]
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(LLMUsageEvent).order_by(LLMUsageEvent.id))).scalars().all()
    assert len(rows) == 2
    assert rows[0].stage == "chat_simple_probe"
    assert rows[0].total_tokens == 100
    assert rows[1].stage == "chat_simple_stream"
    assert rows[1].total_tokens == 150


@pytest.mark.asyncio
async def test_planner_records_usage_event():
    user_id, _ = await _seed_user_and_session()
    task_id = await _seed_task(user_id)
    fake_resp = _fake_response(
        content='[{"title":"Step A","instruction":"Do A","requires_approval":false}]',
        prompt_tokens=60,
        completion_tokens=15,
    )

    with (
        patch("app.autonomy.planner.litellm.acompletion", new=AsyncMock(return_value=fake_resp)),
        patch("app.llm_usage.AsyncSessionLocal", new=TestSessionLocal),
        patch("app.llm_usage.litellm.completion_cost", return_value=0.0015),
    ):
        rows = await _generate_plan_steps(
            goal="G",
            user_id=user_id,
            task_id=task_id,
            task_instruction="I",
            max_steps=3,
            notes="",
            style="concise",
            model_override="gpt-4o",
        )

    assert rows and rows[0]["title"] == "Step A"
    async with TestSessionLocal() as db:
        usage_rows = (await db.execute(select(LLMUsageEvent).where(LLMUsageEvent.source == "task_planner"))).scalars().all()
    assert len(usage_rows) == 1
    assert usage_rows[0].user_id == user_id
    assert usage_rows[0].task_id == task_id
    assert usage_rows[0].total_tokens == 75


@pytest.mark.asyncio
async def test_run_agent_stops_repeated_failed_search_loop_gracefully():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    failing = _fake_tool_response(tool_name="web_search")

    async def _fake_dispatch(_tool_calls, _user_context):
        return [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "No results found for: Buffalo Wild Wings Statesboro GA",
            }
        ]

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch("app.agent.core.litellm.acompletion", new=AsyncMock(side_effect=[failing, failing, failing, failing, failing])),
        patch("app.agent.core.dispatch_tool_calls", side_effect=_fake_dispatch),
    ):
        result = await run_agent([{"role": "user", "content": "Find addresses in Statesboro, GA"}], user_context)

    assert "I couldn't reliably finish that lookup after several search attempts." in result
    assert "No results found for: Buffalo Wild Wings Statesboro GA" in result
    assert "Try narrowing the request or giving me one item at a time." in result


@pytest.mark.asyncio
async def test_run_agent_returns_tool_aware_message_when_hitting_max_turns():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")
    failing = _fake_tool_response(tool_name="web_search")

    async def _fake_dispatch(_tool_calls, _user_context):
        return [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "Partial result: found Zaxby's Statesboro listing but no confirmed phone number.",
            }
        ]

    with (
        patch("app.agent.core.get_tools_for_user", return_value=[]),
        patch("app.agent.core.litellm.acompletion", new=AsyncMock(side_effect=[failing] * 4)),
        patch("app.agent.core.dispatch_tool_calls", side_effect=_fake_dispatch),
        patch.dict("app.agent.core.TURN_LIMITS", {"chat": 4}, clear=False),
        patch("app.agent.core._is_failed_search_turn", return_value=False),
    ):
        result = await run_agent([{"role": "user", "content": "Find restaurant phone numbers in Statesboro, GA"}], user_context)

    assert "I ran out of turns before I could finish that request cleanly." in result
    assert "Partial result: found Zaxby's Statesboro listing but no confirmed phone number." in result


@pytest.mark.asyncio
async def test_run_agent_fails_fast_for_unsupported_alphavantage_weekly_request():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with patch("app.agent.core.litellm.acompletion", new=AsyncMock(side_effect=AssertionError("LLM should not be called"))):
        result = await run_agent(
            [
                {
                    "role": "user",
                    "content": "Use Alpha Vantage to give me weekly bars for SPY.",
                }
            ],
            user_context,
        )

    assert "quote lookup, daily history, and bounded intraday history right now" in result
    assert "supports `global_quote`, `time_series_daily`, and `time_series_intraday`" in result


@pytest.mark.asyncio
async def test_run_agent_does_not_block_supported_alphavantage_daily_request():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with patch("app.agent.core.litellm.acompletion", new=AsyncMock(return_value=_fake_response(content="daily bars here"))):
        result = await run_agent(
            [
                {
                    "role": "user",
                    "content": "Use Alpha Vantage api to fetch daily history for SPY and show the last 30 daily OHLC bars.",
                }
            ],
            user_context,
        )

    assert result == "daily bars here"


@pytest.mark.asyncio
async def test_stream_agent_fails_fast_for_unsupported_alphavantage_indicator_request():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with patch("app.agent.core.litellm.acompletion", new=AsyncMock(side_effect=AssertionError("LLM should not be called"))):
        chunks = [
            chunk
            async for chunk in stream_agent(
                [
                    {
                        "role": "user",
                        "content": "I have an Alpha Vantage key in secrets. Give me the RSI for SPY.",
                    }
                ],
                user_context,
                stage="chat_simple",
            )
        ]

    assert len(chunks) == 1
    assert "quote lookup, daily history, and bounded intraday history right now" in chunks[0]


@pytest.mark.asyncio
async def test_run_agent_does_not_block_supported_intraday_follow_up_request():
    user_context = UserContext(user_id=1, username="tester", role="parent", persona="family_assistant")

    with patch("app.agent.core.litellm.acompletion", new=AsyncMock(return_value=_fake_response(content="intraday bars here"))):
        result = await run_agent(
            [
                {"role": "user", "content": "Use Alpha Vantage for intraday stock data."},
                {"role": "assistant", "content": "I can use Alpha Vantage for quote lookup right now."},
                {"role": "user", "content": "Got it: KO, 60m bars, regular trading hours, CSV."},
            ],
            user_context,
        )

    assert result == "intraday bars here"
