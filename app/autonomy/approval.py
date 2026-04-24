"""
FruitcakeAI v5 — Approval primitives (Phase 4)

Shared module imported by both the task runner (to arm the gate) and the
tool dispatcher (to check it). Using a ContextVar instead of a global means
concurrent tasks never interfere with each other's approval state.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ApprovalRequirement:
    tool_name: str
    reason: str


class ApprovalRequired(Exception):
    """
    Raised by _call_tool() when a task-mode agent tries to invoke an
    approval-gated tool without pre-approval.

    The TaskRunner catches this, sets task.status = "waiting_approval",
    and returns without completing the task. The user approves via
    PATCH /tasks/{id} {"approved": true}, which re-queues the task with
    pre_approved=True so it can proceed.
    """
    def __init__(self, tool_name: str, reason: str | None = None):
        self.tool_name = str(tool_name or "").strip()
        self.reason = reason or approval_reason_for_tool(self.tool_name)
        super().__init__(self.tool_name)

    def __str__(self) -> str:
        # Keep persisted waiting_approval_tool values backward-compatible.
        return self.tool_name


# Tools that must pause for user approval before executing.
# This list covers durable local/external mutations. Conditional mutations are
# handled by approval_requirement_for_tool() below.
APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset({
    "add_memory_observations",
    "add_rss_source",
    "append_file",
    "approve_rss_source_candidate",
    "create_event",
    "create_memory",
    "create_memory_entities",
    "create_memory_relations",
    "create_task_plan",
    "delete_event",
    "discover_rss_sources",
    "make_directory",
    "reject_rss_source_candidate",
    "remove_rss_source",
    "write_file",
    "create_task",
    "update_task",
    "create_and_run_task_plan",
    "run_task_now",
})

CONDITIONAL_APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset({
    "get_daily_market_data",
    "get_intraday_market_data",
})

_APPROVAL_REASONS: dict[str, str] = {
    "add_memory_observations": "Adding memory graph observations changes persisted user memory.",
    "add_rss_source": "Adding an RSS source changes the user's trusted feed catalog.",
    "append_file": "Appending to a workspace file changes persisted user workspace data.",
    "approve_rss_source_candidate": "Approving an RSS candidate changes the user's trusted feed catalog.",
    "create_and_run_task_plan": "Creating and running a task plan can persist task steps and start execution.",
    "create_event": "Creating a calendar event changes an external calendar.",
    "create_memory": "Creating a memory changes persisted user memory.",
    "create_memory_entities": "Creating memory graph entities changes persisted user memory.",
    "create_memory_relations": "Creating memory graph relations changes persisted user memory.",
    "create_task": "Creating a task changes the user's persistent task list.",
    "create_task_plan": "Creating a task plan changes persisted task steps.",
    "delete_event": "Deleting a calendar event changes an external calendar.",
    "discover_rss_sources": "Discovering RSS sources queues persistent feed candidates for review.",
    "get_daily_market_data": "Saving daily market data to the library changes persisted user documents.",
    "get_intraday_market_data": "Saving intraday market data to the library changes persisted user documents.",
    "make_directory": "Creating a workspace directory changes persisted user workspace data.",
    "reject_rss_source_candidate": "Rejecting an RSS candidate changes persistent candidate review state.",
    "remove_rss_source": "Removing an RSS source changes the user's trusted feed catalog.",
    "run_task_now": "Running a task now changes task scheduling and starts execution.",
    "update_task": "Updating a task changes persisted task behavior.",
    "write_file": "Writing a workspace file changes persisted user workspace data.",
}


def approval_reason_for_tool(tool_name: str | None) -> str:
    name = str(tool_name or "").strip()
    return _APPROVAL_REASONS.get(name, "This tool can change persisted user data or external state.")


def approval_requirement_for_tool(
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
) -> ApprovalRequirement | None:
    name = str(tool_name or "").strip()
    if name in APPROVAL_REQUIRED_TOOLS:
        return ApprovalRequirement(tool_name=name, reason=approval_reason_for_tool(name))

    args = arguments or {}
    if name in CONDITIONAL_APPROVAL_REQUIRED_TOOLS and _coerce_bool(args.get("save_to_library")):
        return ApprovalRequirement(tool_name=name, reason=approval_reason_for_tool(name))

    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


# ContextVar: set to True by TaskRunner before calling run_agent() when
# task.requires_approval=True and the task has not been pre-approved.
# Automatically scoped to the current async task — no cross-task leakage.
_approval_armed: ContextVar[bool] = ContextVar("_approval_armed", default=False)
