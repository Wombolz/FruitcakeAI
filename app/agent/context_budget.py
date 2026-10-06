"""Model-aware request budgeting for agent model calls.

This module calculates request capacity and reports where that capacity is
spent. It does not own history or evidence compaction; callers apply their
existing compaction policy using the returned history budget.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping, Sequence

from app.agent.compaction import estimate_history_tokens, estimate_tokens
from app.model_profiles import (
    DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    get_model_profile_service,
)


MINIMUM_INPUT_TOKENS = 2_048

TOOL_EVIDENCE_CLASSES: dict[str, str] = {
    "web_search": "search_index",
    "image_search": "search_index",
    "fetch_page": "source_document",
    "web_context": "provider_context",
    "summarize_document": "document_summary",
    "search_library": "structured_dataset",
    "search_my_feeds": "structured_dataset",
    "search_my_feeds_timeline": "structured_dataset",
    "list_recent_feed_items": "structured_dataset",
}

EVIDENCE_CLASS_PREFERRED_CHARS: dict[str, int] = {
    "ordinary": 8_000,
    "search_index": 8_000,
    "source_document": 20_000,
    "provider_context": 32_000,
    "document_summary": 16_000,
    "structured_dataset": 12_000,
}


@dataclass(frozen=True)
class ModelContextPolicy:
    context_window_tokens: int
    output_reserve_tokens: int
    reasoning_reserve_tokens: int
    safety_margin_tokens: int
    source: str

    @property
    def usable_input_tokens(self) -> int:
        return max(
            MINIMUM_INPUT_TOKENS,
            self.context_window_tokens
            - self.output_reserve_tokens
            - self.reasoning_reserve_tokens
            - self.safety_margin_tokens,
        )


@dataclass(frozen=True)
class RequestBudget:
    model: str
    policy_source: str
    context_window_tokens: int
    output_reserve_tokens: int
    reasoning_reserve_tokens: int
    safety_margin_tokens: int
    usable_input_tokens: int
    message_tokens: int
    history_tokens: int
    fixed_message_tokens: int
    tool_schema_tokens: int
    estimated_input_tokens: int
    history_budget_tokens: int
    estimated_headroom_tokens: int
    over_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskSynthesisEvidenceBudget:
    max_prior_outputs: int
    max_tokens_per_output: int
    max_chars_per_output: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def resolve_model_context_policy(model: str | None) -> ModelContextPolicy:
    profile = get_model_profile_service().for_model(model)
    if profile is None:
        return ModelContextPolicy(
            context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
            output_reserve_tokens=DEFAULT_OUTPUT_RESERVE_TOKENS,
            reasoning_reserve_tokens=0,
            safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN_TOKENS,
            source="conservative_default",
        )
    return ModelContextPolicy(
        context_window_tokens=max(MINIMUM_INPUT_TOKENS, int(profile.context_window_tokens)),
        output_reserve_tokens=max(0, int(profile.output_reserve_tokens)),
        reasoning_reserve_tokens=max(0, int(profile.reasoning_reserve_tokens)),
        safety_margin_tokens=max(0, int(profile.context_safety_margin_tokens)),
        source="model_profile",
    )


def estimate_tool_schema_tokens(tools: Sequence[Mapping[str, Any]] | None) -> int:
    if not tools:
        return 0
    rendered = json.dumps(list(tools), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return estimate_tokens(rendered)


def evidence_class_for_tool(tool_name: str | None) -> str:
    return TOOL_EVIDENCE_CLASSES.get(str(tool_name or "").strip(), "ordinary")


def tool_result_char_budget(
    tool_name: str | None,
    *,
    history_budget_tokens: int,
    ordinary_max_chars: int,
) -> int:
    """Allocate one result no more than roughly a quarter of history space."""
    evidence_class = evidence_class_for_tool(tool_name)
    if evidence_class == "ordinary":
        return max(400, int(ordinary_max_chars))
    preferred = EVIDENCE_CLASS_PREFERRED_CHARS[evidence_class]
    available_share = max(ordinary_max_chars, max(0, int(history_budget_tokens)))
    return max(400, min(preferred, available_share))


def task_synthesis_evidence_budget(model: str | None) -> TaskSynthesisEvidenceBudget:
    policy = resolve_model_context_policy(model)
    usable = policy.usable_input_tokens
    max_outputs = max(2, min(6, usable // 12_000))
    max_tokens = max(300, min(2_000, usable // 64))
    return TaskSynthesisEvidenceBudget(
        max_prior_outputs=max_outputs,
        max_tokens_per_output=max_tokens,
        max_chars_per_output=max_tokens * 4,
    )


def plan_request_budget(
    *,
    model: str,
    request_messages: list[dict[str, Any]],
    history: list[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
) -> RequestBudget:
    policy = resolve_model_context_policy(model)
    message_tokens = estimate_history_tokens(request_messages)
    history_tokens = estimate_history_tokens(history)
    fixed_message_tokens = max(0, message_tokens - history_tokens)
    tool_schema_tokens = estimate_tool_schema_tokens(tools)
    estimated_input_tokens = message_tokens + tool_schema_tokens
    history_budget_tokens = max(
        0,
        policy.usable_input_tokens - fixed_message_tokens - tool_schema_tokens,
    )
    estimated_headroom_tokens = policy.usable_input_tokens - estimated_input_tokens
    return RequestBudget(
        model=str(model or ""),
        policy_source=policy.source,
        context_window_tokens=policy.context_window_tokens,
        output_reserve_tokens=policy.output_reserve_tokens,
        reasoning_reserve_tokens=policy.reasoning_reserve_tokens,
        safety_margin_tokens=policy.safety_margin_tokens,
        usable_input_tokens=policy.usable_input_tokens,
        message_tokens=message_tokens,
        history_tokens=history_tokens,
        fixed_message_tokens=fixed_message_tokens,
        tool_schema_tokens=tool_schema_tokens,
        estimated_input_tokens=estimated_input_tokens,
        history_budget_tokens=history_budget_tokens,
        estimated_headroom_tokens=estimated_headroom_tokens,
        over_budget=estimated_input_tokens > policy.usable_input_tokens,
    )
