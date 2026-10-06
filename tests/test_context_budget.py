from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.agent.context_budget import (
    evidence_class_for_tool,
    estimate_tool_schema_tokens,
    plan_request_budget,
    resolve_model_context_policy,
    task_synthesis_evidence_budget,
    tool_result_char_budget,
)


def _profile(**overrides):
    values = {
        "context_window_tokens": 32_768,
        "output_reserve_tokens": 4_096,
        "reasoning_reserve_tokens": 2_048,
        "context_safety_margin_tokens": 1_024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_request_budget_counts_tool_schemas_and_fixed_prompt_overhead():
    history = [{"role": "user", "content": "H" * 4_000}]
    request_messages = [
        {"role": "system", "content": "S" * 800},
        *history,
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current information.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]

    with patch("app.agent.context_budget.get_model_profile_service") as service:
        service.return_value.for_model.return_value = _profile()
        budget = plan_request_budget(
            model="ollama_chat/test",
            request_messages=request_messages,
            history=history,
            tools=tools,
        )

    assert budget.policy_source == "model_profile"
    assert budget.usable_input_tokens == 25_600
    assert budget.fixed_message_tokens == 200
    assert budget.tool_schema_tokens == estimate_tool_schema_tokens(tools)
    assert budget.history_budget_tokens == 25_600 - 200 - budget.tool_schema_tokens
    assert budget.estimated_headroom_tokens > 0
    assert budget.over_budget is False


def test_request_budget_reports_overflow_before_dispatch():
    history = [{"role": "user", "content": "H" * 40_000}]
    with patch("app.agent.context_budget.get_model_profile_service") as service:
        service.return_value.for_model.return_value = _profile(
            context_window_tokens=8_192,
            output_reserve_tokens=2_048,
            reasoning_reserve_tokens=1_024,
            context_safety_margin_tokens=1_024,
        )
        budget = plan_request_budget(
            model="ollama_chat/small",
            request_messages=history,
            history=history,
            tools=None,
        )

    assert budget.usable_input_tokens == 4_096
    assert budget.history_budget_tokens == 4_096
    assert budget.estimated_headroom_tokens < 0
    assert budget.over_budget is True


def test_unknown_model_uses_conservative_context_policy():
    with patch("app.agent.context_budget.get_model_profile_service") as service:
        service.return_value.for_model.return_value = None
        policy = resolve_model_context_policy("unknown/provider-model")

    assert policy.source == "conservative_default"
    assert policy.context_window_tokens == 65_536
    assert policy.output_reserve_tokens == 8_192
    assert policy.safety_margin_tokens == 2_048


def test_web_evidence_gets_larger_bounded_result_budget():
    assert evidence_class_for_tool("fetch_page") == "source_document"
    assert evidence_class_for_tool("web_context") == "provider_context"
    assert evidence_class_for_tool("unknown_tool") == "ordinary"
    assert tool_result_char_budget(
        "fetch_page",
        history_budget_tokens=40_000,
        ordinary_max_chars=4_000,
    ) == 20_000
    assert tool_result_char_budget(
        "web_context",
        history_budget_tokens=10_000,
        ordinary_max_chars=4_000,
    ) == 10_000


def test_task_synthesis_evidence_budget_scales_with_model_window():
    with patch("app.agent.context_budget.get_model_profile_service") as service:
        service.return_value.for_model.side_effect = [
            _profile(
                context_window_tokens=32_768,
                output_reserve_tokens=4_096,
                reasoning_reserve_tokens=2_048,
                context_safety_margin_tokens=1_024,
            ),
            _profile(
                context_window_tokens=131_072,
                output_reserve_tokens=12_288,
                reasoning_reserve_tokens=8_192,
                context_safety_margin_tokens=4_096,
            ),
        ]
        small = task_synthesis_evidence_budget("small")
        large = task_synthesis_evidence_budget("large")

    assert small.max_prior_outputs == 2
    assert small.max_tokens_per_output == 400
    assert large.max_prior_outputs == 6
    assert large.max_tokens_per_output == 1_664
    assert large.max_chars_per_output == large.max_tokens_per_output * 4
