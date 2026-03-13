"""
Execution profile resolver for task runs.

Today:
- Resolve persona and persona-derived tool allow/block sets.

Future:
- Merge persona + capability profile + user policy + task overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.agent.context import UserContext
from app.agent.persona_router import infer_persona_for_task
from app.agent.persona_loader import persona_exists
from app.agent.tools import get_tools_for_user


@dataclass
class ExecutionProfile:
    persona: str
    allowed_tools: List[str]
    blocked_tools: List[str]
    reason: str = "persona_config"


def resolve_execution_profile(task, user) -> ExecutionProfile:
    """
    Resolve task execution profile.

    Explicit task persona wins; otherwise fall back to user persona and then
    global default.
    """
    explicit = (getattr(task, "persona", None) or "").strip()
    user_default = (getattr(user, "persona", None) or "").strip() or "family_assistant"

    reason = "persona_config"
    if explicit:
        persona_name = explicit
        reason = "task_explicit_persona"
    else:
        inferred, _, infer_reason = infer_persona_for_task(
            getattr(task, "title", "") or "",
            getattr(task, "instruction", "") or "",
            fallback=user_default,
        )
        persona_name = inferred or user_default
        reason = infer_reason

    if not persona_exists(persona_name):
        persona_name = "family_assistant"
        reason = "fallback_invalid_persona"

    ctx = UserContext.from_user(user, persona_name=persona_name)
    tools = get_tools_for_user(ctx)
    allowed_tools = sorted({t["function"]["name"] for t in tools})
    blocked_tools = sorted(set(ctx.blocked_tools))

    return ExecutionProfile(
        persona=persona_name,
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
        reason=reason,
    )
